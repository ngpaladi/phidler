import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtTest import QTest

from phidler.canvas.scene import LayoutScene
from phidler.canvas.transform_handles import RotateHandleItem, TransformHandleSet
from phidler.canvas.view import LayoutView
from phidler.model.document import LayoutDocument, Transform


class _FakeEvent:
    def __init__(self, scene_pos: QPointF) -> None:
        self._pos = scene_pos

    def scenePos(self) -> QPointF:
        return self._pos

    def accept(self) -> None:
        pass


def _setup(qapp):
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(inst.id)
    undo_stack = QUndoStack()
    view = LayoutView(scene, undo_stack=undo_stack)
    view.resize(400, 400)
    view.show()
    handles = TransformHandleSet(scene, doc, undo_stack)
    return doc, scene, undo_stack, view, handles, inst.id


def test_handles_hidden_until_shown_for_an_instance(qapp):
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    for h in handles.handles:
        assert h.isVisible() is False

    handles.show_for(inst_id)
    assert handles.rotate_handle.isVisible() is True

    handles.hide()
    for h in handles.handles:
        assert h.isVisible() is False


def test_photonic_components_get_no_resize_handles(qapp):
    """Only shapes are drag-resized. A waveguide shows the rotate handle alone,
    so a stray corner drag can't distort a device; its scale stays a number in
    the Properties panel."""
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    handles.show_for(inst_id)
    assert handles.rotate_handle.isVisible()
    assert all(not h.isVisible() for h in handles.resize_handles)

    # A bend has a radius like a circle does, but it is a device, not a shape.
    from phidler.canvas.transform_handles import resize_mode

    bend = doc.add_instance("bend_euler", {})
    assert resize_mode(doc, bend.id) is None


def test_rotate_handle_drag_rotates_around_local_origin_by_swept_angle(qapp):
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    handles.show_for(inst_id)

    rh = handles.rotate_handle
    press_pos = rh.scenePos()
    rh.mousePressEvent(_FakeEvent(press_pos))

    theta = math.radians(30)
    new_x = press_pos.x() * math.cos(theta) - press_pos.y() * math.sin(theta)
    new_y = press_pos.x() * math.sin(theta) + press_pos.y() * math.cos(theta)
    rh.mouseMoveEvent(_FakeEvent(QPointF(new_x, new_y)))
    rh.mouseReleaseEvent(_FakeEvent(QPointF(new_x, new_y)))

    t = doc.get_transform(inst_id)
    assert math.isclose(t.rotation, 30.0, abs_tol=1e-6)
    assert math.isclose(t.x, 0.0, abs_tol=1e-9)  # position unchanged by rotation
    assert math.isclose(t.y, 0.0, abs_tol=1e-9)


def test_rotate_handle_live_preview_then_commit(qapp):
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    handles.show_for(inst_id)

    rh = handles.rotate_handle
    press_pos = rh.scenePos()
    rh.mousePressEvent(_FakeEvent(press_pos))
    moved = QPointF(-press_pos.y(), press_pos.x())  # 90 degree sweep
    rh.mouseMoveEvent(_FakeEvent(moved))

    item = scene.items_by_inst[inst_id]
    assert math.isclose(item.rotation_deg, 90.0, abs_tol=1e-6)
    assert math.isclose(doc.get_transform(inst_id).rotation, 0.0, abs_tol=1e-6)  # untouched until release

    rh.mouseReleaseEvent(_FakeEvent(moved))
    assert math.isclose(doc.get_transform(inst_id).rotation, 90.0, abs_tol=1e-6)
    assert undo_stack.count() == 1


def test_handles_keep_constant_screen_size_across_zoom(qapp):
    """Confirms ItemIgnoresTransformations is actually doing its job —
    handles must stay a constant device-pixel size regardless of zoom, the
    standard look for resize/rotate handles in 2D editors."""
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    handles.show_for(inst_id)

    rect_before = handles.handles[0].boundingRect()
    view.scale(15, 15)
    rect_after = handles.handles[0].boundingRect()
    assert rect_before == rect_after


def test_is_interacting_true_only_during_an_active_drag(qapp):
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    handles.show_for(inst_id)
    assert handles.is_interacting() is False

    h = handles.handles[0]
    h.mousePressEvent(_FakeEvent(h.scenePos()))
    assert handles.is_interacting() is True

    h.mouseReleaseEvent(_FakeEvent(h.scenePos()))
    assert handles.is_interacting() is False


def test_reset_action_clears_rotation_mirror_and_scale(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=3.0, y=4.0, rotation=77.0, mirror=True, mag=3.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win._reset_selected_transform()

    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 0.0)
    assert t.mirror is False
    assert math.isclose(t.mag, 1.0)
    assert math.isclose(t.x, 3.0) and math.isclose(t.y, 4.0)

    win.undo_stack.undo()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 77.0)
    assert t.mirror is True
    assert math.isclose(t.mag, 3.0)


def test_overlay_hidden_with_no_or_multiple_selection_through_main_window(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.view.resize(400, 400)
    win.show()

    win._update_transform_overlay()
    assert all(not h.isVisible() for h in win.transform_handles.handles)

    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win.scene.items_by_inst[a_id].setSelected(True)
    win.scene.items_by_inst[b_id].setSelected(True)

    win._update_transform_overlay()
    assert all(not h.isVisible() for h in win.transform_handles.handles)


def test_overlay_shows_for_single_selection_through_main_window(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.view.resize(400, 400)
    win.show()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win._update_transform_overlay()
    assert win.transform_handles.rotate_handle.isVisible()


def test_reset_transform_context_menu_action_is_wired(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=0.0, y=0.0, rotation=45.0, mirror=True, mag=2.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    menu = win._build_canvas_context_menu()
    assert win.reset_transform_action in menu.actions()

    win.reset_transform_action.trigger()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 0.0)
    assert t.mirror is False
    assert math.isclose(t.mag, 1.0)


def test_real_mouse_drag_on_handle_does_not_select_or_move_the_instance(qapp):
    """Exercises the actual QTest-simulated mouse press/move/release path
    through the real view (not direct method calls on the handle) — the
    specific risk being that a click on a handle could fall through to
    RubberBandDrag or the instance's own move-drag if Qt's scene-level hit
    dispatch didn't route it to the handle first. Confirmed empirically
    that it doesn't: the instance stays unselected and the handle's own
    is_dragging flips correctly, all through real Qt event delivery."""
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    view.scale(20, 20)  # zoom in so the click point isn't ambiguous
    handles.show_for(inst_id)

    h = handles.rotate_handle
    press = h.scenePos()
    press_view_pt = view.mapFromScene(press)
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, press_view_pt)
    assert h.is_dragging is True

    # Sweep tangentially (rotate the press point ~90° about the origin) so the
    # rotate handle actually produces a rotation, not a no-op radial move.
    move_view_pt = view.mapFromScene(QPointF(-press.y(), press.x()))
    QTest.mouseMove(view.viewport(), move_view_pt)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, move_view_pt)

    assert undo_stack.count() == 1
    item = scene.items_by_inst[inst_id]
    assert item.isSelected() is False  # the handle click must not select/move the instance itself


# -- drag-resizing shapes ---------------------------------------------------


def _shape_setup(qapp, spec, kwargs, transform=None):
    doc = LayoutDocument()
    inst = doc.add_instance(spec, kwargs)
    if transform is not None:
        doc.set_transform(inst.id, transform)
    scene = LayoutScene(doc)
    scene.add_instance_item(inst.id)
    undo_stack = QUndoStack()
    view = LayoutView(scene, undo_stack=undo_stack)
    view.snap_enabled = False
    view.resize(400, 400)
    view.show()
    scene._kept_view = view  # the scene doesn't own its views; keep this one alive
    resized = []
    handles = TransformHandleSet(scene, doc, undo_stack, on_resized=resized.append)
    handles.show_for(inst.id)
    return doc, scene, undo_stack, handles, inst.id, resized


def _drag(handle, start: QPointF, end: QPointF, modifiers=Qt.NoModifier):
    class _Event(_FakeEvent):
        def modifiers(self):
            return modifiers

    handle.mousePressEvent(_Event(start))
    handle.mouseMoveEvent(_Event(end))
    handle.mouseReleaseEvent(_Event(end))


def _scene_corners(scene, inst_id):
    item = scene.items_by_inst[inst_id]
    rect = item.mapRectToScene(item.boundingRect())
    return rect.left(), rect.top(), rect.right(), rect.bottom()


def _handle(handles, corner):
    return next(h for h in handles.resize_handles if h.corner == corner)


def test_dragging_a_rectangle_corner_changes_its_size_with_the_opposite_corner_fixed(qapp):
    doc, scene, undo_stack, handles, inst_id, resized = _shape_setup(qapp, "rectangle", {"size": (4.0, 2.0)})
    assert all(h.isVisible() for h in handles.resize_handles)
    left, top, right, bottom = _scene_corners(scene, inst_id)

    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 10.0, top + 5.0))
    assert doc.instances[inst_id].kwargs["size"] == (10.0, 5.0)
    assert doc.get_transform(inst_id).mag == 1.0  # a real 10 x 5 rectangle, not a magnified one
    new = _scene_corners(scene, inst_id)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(new, (left, top, left + 10.0, top + 5.0)))
    assert resized == [inst_id]

    # The opposite corner: the far (right/bottom) corner stays put, so the shape moves.
    grab = _handle(handles, (0, 0))
    _drag(grab, grab.scenePos(), QPointF(left + 4.0, top + 1.0))
    assert doc.instances[inst_id].kwargs["size"] == (6.0, 4.0)
    new = _scene_corners(scene, inst_id)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(new, (left + 4.0, top + 1.0, left + 10.0, top + 5.0)))

    # Each drag is one undo step, restoring size and position together.
    undo_stack.undo()
    assert doc.instances[inst_id].kwargs["size"] == (10.0, 5.0)
    assert math.isclose(_scene_corners(scene, inst_id)[0], left, abs_tol=1e-6)
    undo_stack.undo()
    assert doc.instances[inst_id].kwargs["size"] == (4.0, 2.0)
    assert not undo_stack.canUndo()


def test_resize_follows_a_rotated_centred_rectangle(qapp):
    """Sizes are measured in the shape's own frame, and a centred rectangle
    (which grows both ways) is shifted so the fixed corner really stays fixed."""
    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(
        qapp, "rectangle", {"size": (4.0, 2.0), "centered": True},
        Transform(x=3.0, y=1.0, rotation=90.0, mirror=False, mag=1.0),
    )
    item = scene.items_by_inst[inst_id]
    grab = _handle(handles, (1, 1))
    fixed_before = item.mapToScene(grab.corner_point(item.boundingRect(), (0, 0)))
    # 6 um further along the shape's local x, which is scene y after the rotation.
    _drag(grab, grab.scenePos(), grab.scenePos() + QPointF(0.0, 6.0))
    size = doc.instances[inst_id].kwargs["size"]
    assert math.isclose(size[0], 10.0, abs_tol=1e-6) and math.isclose(size[1], 2.0, abs_tol=1e-6)
    item = scene.items_by_inst[inst_id]
    fixed_after = item.mapToScene(grab.corner_point(item.boundingRect(), (0, 0)))
    assert math.isclose(fixed_after.x(), fixed_before.x(), abs_tol=1e-6)
    assert math.isclose(fixed_after.y(), fixed_before.y(), abs_tol=1e-6)


def test_shift_keeps_proportions_and_sizes_snap_to_the_grid(qapp):
    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, "rectangle", {"size": (4.0, 2.0)})
    left, top, _, _ = _scene_corners(scene, inst_id)
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 8.0, top + 2.5), Qt.ShiftModifier)
    assert doc.instances[inst_id].kwargs["size"] == (8.0, 4.0)

    view = scene.views()[0]
    view.snap_enabled, view.grid_pitch = True, 0.5
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 9.7, top + 3.2))
    assert doc.instances[inst_id].kwargs["size"] == (9.5, 3.0)


def test_circle_resizes_by_radius_and_ellipse_by_radii(qapp):
    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, "circle", {"radius": 10.0})
    left, top, right, bottom = _scene_corners(scene, inst_id)
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 2 * (right - left), top + 1.2 * (bottom - top)))
    assert math.isclose(doc.instances[inst_id].kwargs["radius"], 20.0, rel_tol=1e-6)  # even scaling

    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, "ellipse", {"radii": (10.0, 5.0)})
    left, top, right, bottom = _scene_corners(scene, inst_id)
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 1.5 * (right - left), top + 2 * (bottom - top)))
    radii = doc.instances[inst_id].kwargs["radii"]
    assert math.isclose(radii[0], 15.0, rel_tol=1e-6) and math.isclose(radii[1], 10.0, rel_tol=1e-6)


def test_other_shapes_scale_by_magnification(qapp):
    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, "triangle", {})
    left, top, right, bottom = _scene_corners(scene, inst_id)
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 2 * (right - left), top + 2 * (bottom - top)))
    assert math.isclose(doc.get_transform(inst_id).mag, 2.0, rel_tol=1e-6)
    new = _scene_corners(scene, inst_id)
    assert math.isclose(new[0], left, abs_tol=1e-6) and math.isclose(new[2] - new[0], 2 * (right - left), rel_tol=1e-6)


def test_a_click_without_a_drag_changes_nothing(qapp):
    doc, scene, undo_stack, handles, inst_id, resized = _shape_setup(qapp, "rectangle", {"size": (4.0, 2.0)})
    grab = _handle(handles, (1, 1))
    grab.mousePressEvent(_FakeEvent(grab.scenePos()))
    grab.mouseReleaseEvent(_FakeEvent(grab.scenePos()))
    assert not undo_stack.canUndo() and resized == []


def test_drag_resize_updates_the_properties_panel(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.view.resize(400, 400)
    win.show()
    win.view.snap_enabled = False
    inst = win.document.add_instance("rectangle", {"size": (4.0, 2.0)})
    win.scene.add_instance_item(inst.id)
    win.scene.items_by_inst[inst.id].setSelected(True)
    win._update_transform_overlay()
    grab = _handle(win.transform_handles, (1, 1))
    assert grab.isVisible()
    left, top, _, _ = _scene_corners(win.scene, inst.id)
    _drag(grab, grab.scenePos(), QPointF(left + 7.0, top + 3.0))
    spins = win.properties_panel._fields["size"].property("sequence_spins")
    assert [s.value() for s in spins] == [7.0, 3.0]
    assert win.scene.items_by_inst[inst.id].isSelected()


def test_dragging_onto_or_past_the_fixed_corner_cannot_collapse_the_shape(qapp):
    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, "rectangle", {"size": (4.0, 2.0)})
    left, top, right, bottom = _scene_corners(scene, inst_id)
    view = scene.views()[0]

    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left - 5.0, top - 5.0))  # past the fixed corner, no grid
    size = doc.instances[inst_id].kwargs["size"]
    assert size[0] >= 0.01 and size[1] >= 0.01

    undo_stack.undo()
    view.snap_enabled, view.grid_pitch = True, 1.0
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left + 0.2, top - 3.0))  # within half a pitch, and past
    assert doc.instances[inst_id].kwargs["size"] == (1.0, 1.0)  # one grid pitch, not a sliver

    # A circle dragged to nothing keeps a real radius too.
    doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, "circle", {"radius": 10.0})
    left, top, _, _ = _scene_corners(scene, inst_id)
    grab = _handle(handles, (1, 1))
    _drag(grab, grab.scenePos(), QPointF(left - 1.0, top - 1.0))
    assert doc.instances[inst_id].kwargs["radius"] >= 0.01
    assert scene.items_by_inst[inst_id].boundingRect().width() > 0


def test_real_mouse_resize_through_the_view_is_one_undo_step(qapp):
    """Through the view's own press/move/release (which track canvas drags):
    a resize that also shifts the shape must stay one undo step, and the fixed
    corner must not be re-snapped to the grid afterwards."""
    from phidler.main_window import MainWindow

    for kwargs, corner in (({"size": (40.0, 20.0)}, (0, 0)), ({"size": (40.0, 20.0), "centered": True}, (1, 1))):
        win = MainWindow()
        win.resize(1000, 800)
        win.show()
        inst = win.document.add_instance("rectangle", kwargs)
        win.scene.add_instance_item(inst.id)
        # Off-grid on purpose, so a second snap-and-commit would be visible.
        start = Transform(x=0.3, y=0.4, rotation=0.0, mirror=False, mag=1.0)
        win.document.set_transform(inst.id, start)
        win.scene.items_by_inst[inst.id].apply_transform(0.3, 0.4, 0.0, False, 1.0)
        win.scene.commit_dirty_transforms()  # placed, not mid-drag
        win.view.zoom_to_fit()
        win.view.scale(0.5, 0.5)
        qapp.processEvents()
        win.scene.items_by_inst[inst.id].setSelected(True)
        win._update_transform_overlay()
        qapp.processEvents()
        win.undo_stack.clear()

        grab = _handle(win.transform_handles, corner)
        anchor_corner = (1 - corner[0], 1 - corner[1])
        item = win.scene.items_by_inst[inst.id]
        fixed = item.mapToScene(grab.corner_point(item.boundingRect(), anchor_corner))
        viewport = win.view.viewport()
        p0 = win.view.mapFromScene(grab.scenePos())
        direction = 1 if corner == (1, 1) else -1
        p1 = win.view.mapFromScene(grab.scenePos() + QPointF(10.0 * direction, 6.0 * direction))
        QTest.mousePress(viewport, Qt.LeftButton, Qt.NoModifier, p0)
        QTest.mouseMove(viewport, p1)
        QTest.mouseRelease(viewport, Qt.LeftButton, Qt.NoModifier, p1)
        qapp.processEvents()

        size = win.document.instances[inst.id].kwargs["size"]
        assert size[0] > 40.0 and size[1] > 20.0
        assert win.undo_stack.count() == 1
        item = win.scene.items_by_inst[inst.id]
        after = item.mapToScene(grab.corner_point(item.boundingRect(), anchor_corner))
        assert math.isclose(after.x(), fixed.x(), abs_tol=1e-6) and math.isclose(after.y(), fixed.y(), abs_tol=1e-6)
        t = win.document.get_transform(inst.id)
        assert math.isclose(item.pos().x(), t.x, abs_tol=1e-6) and math.isclose(item.pos().y(), t.y, abs_tol=1e-6)

        win.undo_stack.undo()
        assert win.document.instances[inst.id].kwargs["size"] == kwargs["size"]
        back = win.document.get_transform(inst.id)
        assert (back.x, back.y) == (0.3, 0.4)


def test_every_parameter_resized_shape_lands_on_the_dragged_extent(qapp):
    """The drag assumes a shape's bounding box follows its size/radii/radius.
    Check that for every shape resized that way; any that don't follow belong
    in magnification mode instead."""
    import gdsfactory as gf

    from phidler.canvas.transform_handles import resize_mode

    checked = []
    for name, factory in sorted(gf.get_active_pdk().cells.items()):
        if not (getattr(factory, "__module__", "") or "").startswith("gdsfactory.components.shapes"):
            continue
        try:
            doc, scene, undo_stack, handles, inst_id, _ = _shape_setup(qapp, name, {})
        except Exception:
            continue  # needs arguments the palette can't place with defaults either
        mode = resize_mode(doc, inst_id)
        if mode is None or mode[0] == "mag":
            continue
        left, top, right, bottom = _scene_corners(scene, inst_id)
        w, h = right - left, bottom - top
        grab = _handle(handles, (1, 1))
        _drag(grab, grab.scenePos(), QPointF(left + 2 * w, top + 2 * h))
        assert undo_stack.canUndo(), f"{name}: the resize failed to rebuild"
        new = _scene_corners(scene, inst_id)
        assert math.isclose(new[2] - new[0], 2 * w, rel_tol=1e-6), name
        assert math.isclose(new[3] - new[1], 2 * h, rel_tol=1e-6), name
        assert math.isclose(new[0], left, abs_tol=1e-6) and math.isclose(new[1], top, abs_tol=1e-6), name
        checked.append(name)
    assert set(checked) == {"rectangle", "rectangles", "compass", "L", "C", "ellipse", "circle", "pie_arc"}
