import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.model.document import LayoutDocument, Transform


def _drag(view, press_scene_pt: QPointF, release_scene_pt: QPointF) -> None:
    press_view_pt = view.mapFromScene(press_scene_pt)
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, press_view_pt)
    release_view_pt = view.mapFromScene(release_scene_pt)
    QTest.mouseMove(view.viewport(), release_view_pt)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, release_view_pt)


def test_dragging_a_port_near_another_snaps_to_exact_alignment(qapp):
    """straight's ports are o1 at local (0,0) and o2 at local (10,0). A is
    placed at the origin (o2 absolute at (10, 0)). B starts far away;
    dragging it so its o1 lands within the snap threshold of A's o2 must
    snap that port to land exactly on (10, 0), not just grid-round."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(a.id)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=50.0, y=50.0)
    item_b = scene.add_instance_item(b.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    # B's o1 (local (0,0)) is currently at B's position, (50, 50). Drag B
    # by (-39.5, -49.7) so o1 would land at (10.5, 0.3) — within the 2um
    # threshold of A's o2 at (10, 0), but not exactly on it.
    press_pt = QPointF(55.0, 50.0)  # somewhere on B
    release_pt = QPointF(55.0 - 39.5, 50.0 - 49.7)
    _drag(view, press_pt, release_pt)

    t_b = doc.get_transform(b.id)
    # B moved by the snap offset, so its position is exactly 39.5/49.7 off
    # from a pure grid-round of the release point — confirms the snap
    # logic (not grid-snap) determined the final position.
    abs_ports = doc.get_absolute_ports_for_instance(b.id)
    o1 = next((x, y) for name, x, y in abs_ports if name == "o1")
    assert math.isclose(o1[0], 10.0, abs_tol=1e-6)
    assert math.isclose(o1[1], 0.0, abs_tol=1e-6)


def test_dragging_far_from_any_port_falls_back_to_grid_snap(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(a.id)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=500.0, y=500.0)
    scene.add_instance_item(b.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()
    view.grid_pitch = 1.0

    # drag B somewhere nowhere near A's ports, ending at a non-integer
    # position — must fall back to ordinary grid-snap (round to nearest um)
    press_pt = QPointF(505.0, 500.0)
    release_pt = QPointF(200.3, 200.7)
    _drag(view, press_pt, release_pt)

    t_b = doc.get_transform(b.id)
    assert math.isclose(t_b.x, round(t_b.x), abs_tol=1e-9)
    assert math.isclose(t_b.y, round(t_b.y), abs_tol=1e-9)


def test_multi_select_drag_shifts_whole_group_by_snap_offset(qapp):
    """When multiple items are dragged together and one of them finds a
    port match, the SAME offset must apply to the whole group — not just
    the one instance whose port matched — so their relative arrangement
    is preserved."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(a.id)

    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=50.0, y=50.0)
    item_b = scene.add_instance_item(b.id)
    c = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=50.0, y=100.0)
    item_c = scene.add_instance_item(c.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    item_b.setSelected(True)
    item_c.setSelected(True)

    press_pt = QPointF(55.0, 50.0)  # on B
    release_pt = QPointF(55.0 - 39.5, 50.0 - 49.7)  # B's o1 within threshold of A's o2
    _drag(view, press_pt, release_pt)

    t_b = doc.get_transform(b.id)
    t_c = doc.get_transform(c.id)
    # C must have moved by the exact same offset as B, preserving the
    # original 50-unit separation between them
    assert math.isclose(t_c.y - t_b.y, 50.0, abs_tol=1e-6)


def test_find_port_snap_offset_returns_none_with_no_other_instances(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(a.id)
    view = LayoutView(scene)
    assert view._find_port_snap_offset([a.id]) is None


def test_find_port_snap_offset_returns_none_for_empty_dragged_list(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    assert view._find_port_snap_offset([]) is None


def test_snapping_can_be_disabled(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(a.id)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=50.0, y=50.0)
    scene.add_instance_item(b.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()
    view.snap_enabled = False

    press_pt = QPointF(55.0, 50.0)
    release_pt = QPointF(55.0 - 39.5, 50.0 - 49.7)
    _drag(view, press_pt, release_pt)

    t_b = doc.get_transform(b.id)
    # with snapping off entirely, B lands at the raw release-implied
    # position, not snapped to A's port
    assert not math.isclose(t_b.x, 10.0, abs_tol=1e-3)
