from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from phidler.main_window import MainWindow
from phidler.model.document import Transform


def _place_two_straights(win):
    """Places A at the origin and B offset with a real gap + a 90-degree
    turn, so route_single has actual bend/straight segments to insert
    (head-to-head ports with zero gap route to nothing — verified separately
    against gdsfactory directly)."""
    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win.document.set_transform(b_id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    win.scene.items_by_inst[b_id].apply_transform(0.0, 20.0, 90.0, False)
    return a_id, b_id


def test_routing_mode_toggle_and_port_click_creates_route(qapp):
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)

    win.route_action.setChecked(True)
    assert win.scene.routing_mode is True

    win._on_port_clicked(a_id, "o2")
    assert win._pending_route_port == (a_id, "o2")

    win._on_port_clicked(b_id, "o1")
    assert win._pending_route_port is None
    assert len(win.document.routes) == 1
    route_id = next(iter(win.document.routes))
    assert route_id in win.scene.route_items


def test_route_click_through_real_view_nearest_port(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    a_id, b_id = _place_two_straights(win)

    # zoom in so a click "near" a port (close to the shape's edge) doesn't
    # round, at the viewport's integer-pixel granularity, onto the exact
    # boundary edge where Qt's precise path hit-test can reject it
    win.view.scale(20, 20)

    win.route_action.setChecked(True)

    # click near a's o2 port (scene point (10, 0))
    view_pt_a = win.view.mapFromScene(QPointF(9.9, 0.0))
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, view_pt_a)
    assert win._pending_route_port == (a_id, "o2")

    # click near b's o1 port (scene point (0, 20) — b is rotated 90deg so its
    # body extends from y=20 to y=30, i.e. just *above* 20, not below)
    view_pt_b = win.view.mapFromScene(QPointF(0.0, 20.1))
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, view_pt_b)
    assert len(win.document.routes) == 1


def test_port_click_tolerance_is_zoom_aware(qapp):
    """The whole bug: a fixed 0.6µm hit radius is sub-pixel when zoomed out,
    so port clicks never registered. The tolerance must scale with zoom."""
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    a_id, _b_id = _place_two_straights(win)

    # Zoomed out (small scale): a generous scene-space tolerance.
    win.view.resetTransform()
    win.view.scale(0.5, 0.5)
    tol_out = win.view._port_click_tolerance_scene()
    # Zoomed in: a tight scene-space tolerance (same pixels, finer µm).
    win.view.resetTransform()
    win.view.scale(50, 50)
    tol_in = win.view._port_click_tolerance_scene()
    assert tol_out > tol_in

    # When zoomed out, a click ~1µm off a's o2 port (10, 0) still finds it —
    # this is exactly the click the old fixed 0.6µm radius rejected.
    win.view.resetTransform()
    win.view.scale(0.5, 0.5)
    hit = win.view._nearest_port_for_routing(QPointF(11.0, 0.0))
    assert hit == (a_id, "o2")
    # A click far from any port returns nothing.
    assert win.view._nearest_port_for_routing(QPointF(500.0, 500.0)) is None


def test_escape_cancels_an_armed_placement(qapp):
    """Esc backs out of a placement armed from the palette — the same cancel
    the focus-independent window shortcut drives."""
    win = MainWindow()
    win.view.arm_placement("straight")
    assert win.view.armed_component == "straight"
    assert win.view.cancel_current_action() is True
    assert win.view.armed_component is None


def test_escape_is_two_stage_for_routing(qapp):
    """First Esc drops the half-finished route (the picked start port) but
    stays in routing mode; a second Esc exits routing mode."""
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    a_id, b_id = _place_two_straights(win)
    win.route_action.setChecked(True)
    win._on_port_clicked(a_id, "o2")
    assert win._pending_route_port == (a_id, "o2")
    assert win.view._route_anchor is not None

    win.view.cancel_current_action()  # first Esc
    assert win._pending_route_port is None
    assert win.view._route_anchor is None
    assert win.scene.routing_mode is True  # still routing — only the pick was dropped

    win.view.cancel_current_action()  # second Esc
    assert win.scene.routing_mode is False
    assert win.route_action.isChecked() is False


def test_escape_exits_routing_mode_and_unchecks_toolbar_button(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()

    win.route_action.setChecked(True)
    assert win.scene.routing_mode is True

    QTest.keyClick(win.view, Qt.Key_Escape)
    assert win.scene.routing_mode is False
    assert win.route_action.isChecked() is False


def test_route_undo_redo_removes_and_restores_refs(qapp):
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route_id = next(iter(win.document.routes))
    n_insts_with_route = len(list(win.document.top.insts))

    win.undo_stack.undo()
    assert route_id not in win.document.routes
    assert route_id not in win.scene.route_items
    assert len(list(win.document.top.insts)) < n_insts_with_route

    win.undo_stack.redo()
    assert route_id in win.document.routes
    assert route_id in win.scene.route_items


def test_delete_selected_route_is_undoable(qapp):
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route_id = next(iter(win.document.routes))

    win.scene.clearSelection()
    win.scene.route_items[route_id].setSelected(True)
    win._delete_selected()

    assert route_id not in win.document.routes

    win.undo_stack.undo()
    assert route_id in win.document.routes


def test_delete_instance_and_route_together_undoes_cleanly(qapp):
    """QUndoStack undoes a macro's children in reverse push order. Deleting
    an instance and the route attached to it in the same macro, then
    undoing, must restore both — not abort partway through because the
    route's undo() tried to look up an endpoint instance that wasn't back
    yet (confirmed empirically before the fix: the route's undo() raised
    KeyError and the instance's own undo() never ran)."""
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route_id = next(iter(win.document.routes))

    win.scene.items_by_inst[a_id].setSelected(True)
    win.scene.route_items[route_id].setSelected(True)
    win._delete_selected()

    assert a_id not in win.document.instances
    assert route_id not in win.document.routes

    win.undo_stack.undo()

    assert a_id in win.document.instances
    assert b_id in win.document.instances
    assert route_id in win.document.routes
    assert a_id in win.scene.items_by_inst
    assert route_id in win.scene.route_items


def test_deleting_instance_then_separately_undoing_orphaned_route_delete_fails_cleanly(qapp):
    """If an instance is deleted without its route (no cascade-delete is a
    known v1 limitation), the route survives pointing at a gone endpoint.
    Deleting that orphaned route and then undoing must not crash — there's
    nothing valid to restore it to, so the undo should be a clean no-op
    with the failure recorded on the command, not an uncaught KeyError."""
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route_id = next(iter(win.document.routes))

    win.scene.items_by_inst[a_id].setSelected(True)
    win._delete_selected()  # delete only the instance, route survives orphaned
    assert route_id in win.document.routes

    win.scene.clearSelection()
    win.scene.route_items[route_id].setSelected(True)
    win._delete_selected()
    assert route_id not in win.document.routes

    win.undo_stack.undo()  # undoing the orphaned route's deletion must not raise
    assert route_id not in win.document.routes  # restore is impossible; stays gone


def test_route_export_includes_route_geometry(qapp, tmp_path):
    import gdsfactory as gf

    win = MainWindow()
    a_id, b_id = _place_two_straights(win)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")

    out = tmp_path / "with_route.gds"
    win.document.export_gds(str(out))
    reimported = gf.import_gds(str(out))
    # 2 straights with no gap routed directly should just be a short/zero-length
    # connector; regardless, the export must not crash and must contain geometry
    assert not reimported.bbox().empty()


def test_routes_use_straight_sections_and_euler_bends(qapp):
    """Locks in "straight sections and adiabatic turns": confirmed by
    inspecting route_single's actual signature that its `bend` parameter
    already defaults to `bend_euler` (continuously-varying curvature —
    the standard low-loss "adiabatic" turn in photonics, not a separately
    named "adiabatic" component) and that our add_route call never
    overrides it. This test checks the *actual* cell names a real route
    produces, not just the default parameter value, so a future change
    that accidentally passes a different `bend=` would be caught here."""
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)  # places with a real gap + 90-degree turn
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route_id = next(iter(win.document.routes))
    route = win.document.routes[route_id]

    cell_names = [ref.cell.name for ref in route.refs]
    assert any("bend_euler" in name for name in cell_names)
    assert any("straight" in name for name in cell_names)
    assert not any("bend_circular" in name for name in cell_names)


def test_clicking_far_from_any_port_does_nothing(qapp):
    win = MainWindow()
    a_id, _b_id = _place_two_straights(win)
    win.route_action.setChecked(True)

    item = win.scene.items_by_inst[a_id]
    far_point = item.mapFromScene(QPointF(1000.0, 1000.0))
    assert item.nearest_port(far_point) is None


def test_route_length_readout_reports_um_and_propagation_time(qapp):
    win = MainWindow()
    a_id, b_id = _place_two_straights(win)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route = next(iter(win.document.routes.values()))

    # PlacedRoute.length is in nm; route_length_um converts via the layout dbu.
    length_um = win.route_length_um(route)
    assert length_um > 0
    # A straight-ish ~20-30µm route, not a nm-scale number from forgetting dbu.
    assert 1.0 < length_um < 1000.0

    # Selecting the route surfaces a status-bar readout with both units.
    win.scene.route_items[route.id].setSelected(True)
    win._show_route_readout()
    msg = win.statusBar().currentMessage()
    assert "µm" in msg and ("fs" in msg or "ps" in msg)


def test_routing_shows_hover_highlight_and_preview_track(qapp):
    from PySide6.QtCore import QPointF

    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    a_id, b_id = _place_two_straights(win)
    win.route_action.setChecked(True)

    # Hovering near a's o2 (10,0) highlights that port.
    snap_pt = win.view._update_hover_port(QPointF(10.0, 0.0))
    assert snap_pt is not None
    assert win.view._hover_port_item is not None and win.view._hover_port_item.isVisible()

    # After the first port click, a preview track anchor + line exist.
    win._on_port_clicked(a_id, "o2")
    assert win.view._route_anchor is not None
    win.view._update_route_preview(QPointF(5.0, 5.0))
    assert win.view._route_preview_item is not None

    # Completing the route clears the preview.
    win._on_port_clicked(b_id, "o1")
    assert win.view._route_anchor is None
    assert win.view._route_preview_item is None
