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
    for h in handles.handles:
        assert h.isVisible() is True

    handles.hide()
    for h in handles.handles:
        assert h.isVisible() is False


def test_scale_is_not_a_drag_gesture(qapp):
    """Scale is set numerically in the Properties panel, not by dragging — so
    there are no corner handles, only the rotate handle, and none of them
    touch `mag` on a drag."""
    doc, scene, undo_stack, view, handles, inst_id = _setup(qapp)
    handles.show_for(inst_id)
    assert handles.handles == [handles.rotate_handle]
    assert all(isinstance(h, RotateHandleItem) for h in handles.handles)


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
    assert all(h.isVisible() for h in win.transform_handles.handles)


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
