import math

from phidler.main_window import MainWindow


def test_place_select_rotate_mirror_through_main_window(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    item = win.scene.items_by_inst[inst_id]
    item.setSelected(True)

    win._rotate_selected()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 90.0)

    win._flip_selected("v")
    t = win.document.get_transform(inst_id)
    assert (t.rotation, t.mirror) != (90.0, False)  # the flip changed the orientation

    win.undo_stack.undo()  # undo flip
    win.undo_stack.undo()  # undo rotate
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 0.0)
    assert t.mirror is False


def test_rotate_and_flip_preserve_scale(qapp):
    """Transform.mag defaults to 1.0, so _rotate_selected/_flip_selected
    constructing a new Transform without explicitly copying mag would silently
    reset any applied scale back to 100% on every rotate or flip — exactly the
    kind of "new dataclass field, old code that doesn't know about it" bug this
    test exists to catch."""
    from phidler.model.document import Transform

    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    old_t = win.document.get_transform(inst_id)
    win.document.set_transform(inst_id, Transform(x=old_t.x, y=old_t.y, rotation=old_t.rotation, mirror=old_t.mirror, mag=2.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win._rotate_selected()
    assert math.isclose(win.document.get_transform(inst_id).mag, 2.0)

    win._flip_selected("h")
    assert math.isclose(win.document.get_transform(inst_id).mag, 2.0)

    win._flip_selected("v")
    assert math.isclose(win.document.get_transform(inst_id).mag, 2.0)


def test_copy_paste_through_main_window(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win._copy_selected()
    assert len(win._clipboard) == 1

    win._paste_clipboard()
    assert len(win.document.instances) == 2


def test_delete_selected_through_main_window_is_undoable(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win._delete_selected()
    assert inst_id not in win.document.instances

    win.undo_stack.undo()
    assert inst_id in win.document.instances
    assert inst_id in win.scene.items_by_inst


def test_layer_visibility_and_color_signals_update_scene(qapp):
    from phidler.model.document import Transform

    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    other_id = [i for i in win.document.instances if i != inst_id][0]
    win.document.set_transform(other_id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    win.scene.items_by_inst[other_id].apply_transform(0.0, 20.0, 90.0, False)
    win._on_port_clicked(inst_id, "o2")
    win._on_port_clicked(other_id, "o1")
    route_id = next(iter(win.document.routes))

    item = win.scene.items_by_inst[inst_id]
    wg_key = (1, 0)
    poly_item = item._layer_children[wg_key][0]
    route_item = win.scene.route_items[route_id]
    route_poly_item = route_item._layer_children[wg_key][0]

    win._on_layer_visibility_changed(wg_key, False)
    assert poly_item.isVisible() is False
    assert route_poly_item.isVisible() is False  # routes share the WG layer too
    assert win.document.layers[wg_key].visible is False

    win._on_layer_color_changed(wg_key, "#ff0000")
    assert win.document.layers[wg_key].color == "#ff0000"
    assert poly_item.brush().color().name() == "#ff0000"
    assert route_poly_item.brush().color().name() == "#ff0000"


def test_undo_redo_actions_are_wired(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    assert len(win.document.instances) == 1
    win.undo_stack.undo()
    assert len(win.document.instances) == 0
    win.undo_stack.redo()
    assert len(win.document.instances) == 1


def test_select_all_selects_instances_and_routes(qapp):
    from phidler.model.document import Transform

    win = MainWindow()
    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win.document.set_transform(b_id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    win.scene.items_by_inst[b_id].apply_transform(0.0, 20.0, 90.0, False)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    route_id = next(iter(win.document.routes))

    assert not win.scene.items_by_inst[a_id].isSelected()
    win._select_all()

    assert win.scene.items_by_inst[a_id].isSelected()
    assert win.scene.items_by_inst[b_id].isSelected()
    assert win.scene.route_items[route_id].isSelected()


def test_layers_panel_starts_empty_and_only_shows_layers_actually_used(qapp):
    """Real complaint from using the app: the layers panel used to be
    pre-seeded with the active PDK's entire ~47-layer map regardless of
    what was actually placed, which is overwhelming and mostly irrelevant
    clutter for a typical design that only touches a handful of layers."""
    win = MainWindow()
    assert win.document.layers == {}
    assert win.layers_panel.list_widget.count() == 0

    win._place_straight_waveguide()  # 'straight' only uses the WG layer (1, 0)

    assert set(win.document.layers.keys()) == {(1, 0)}


def test_apply_project_settings_updates_document_route_default_and_combo(qapp):
    from phidler.model.document import ProjectSettings

    win = MainWindow()
    assert win.route_cross_section == "strip"

    settings = ProjectSettings(
        platform_name="Silicon Nitride (SiN)",
        core_index=2.0,
        clad_index=1.44,
        thickness_um=0.4,
        wavelength_um=1.55,
        cross_section="nitride",
    )
    win._apply_project_settings(settings)

    assert win.document.project_settings is settings
    assert win.route_cross_section == "nitride"
    assert win.cross_section_combo.currentText() == "nitride"


def test_reset_to_new_project_clears_content_and_applies_settings(qapp):
    from phidler.model.document import ProjectSettings

    win = MainWindow()
    win._place_straight_waveguide()
    assert len(win.document.instances) == 1

    settings = ProjectSettings(cross_section="nitride")
    win._reset_to_new_project(settings)

    assert len(win.document.instances) == 0
    assert win.document.project_settings is settings
    assert win.route_cross_section == "nitride"
    assert win.project_path is None


def test_project_settings_menu_action_is_wired(qapp):
    from PySide6.QtWidgets import QMenu

    win = MainWindow()
    # Find the "Project Settings…" action in the File menu without
    # invoking it (it opens a blocking modal dialog).
    file_menu = next(m for m in win.menuBar().findChildren(QMenu) if m.title() == "&File")
    titles = [a.text() for a in file_menu.actions()]
    assert any("Project Settings" in t for t in titles)
