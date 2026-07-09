import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.model.document import LayoutDocument


def test_click_select_and_drag_through_real_view(qapp):
    """Exercises the actual mouse-driven interaction path (click to select,
    drag to move, release to commit) through LayoutView, rather than poking
    the model/scene APIs directly."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    item = scene.add_instance_item(inst.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()
    assert not item.isSelected()

    # click near the middle of the waveguide (scene point (5, 0)) and drag it
    press_scene_pt = QPointF(5.0, 0.0)
    press_view_pt = view.mapFromScene(press_scene_pt)
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, press_view_pt)

    target_scene_pt = QPointF(15.0, 4.0)
    move_view_pt = view.mapFromScene(target_scene_pt)
    QTest.mouseMove(view.viewport(), move_view_pt)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, move_view_pt)

    assert item.isSelected()

    transform = doc.get_transform(inst.id)
    assert math.isclose(transform.x, 10.0, abs_tol=1e-6)
    assert math.isclose(transform.y, 4.0, abs_tol=1e-6)


def test_dragging_one_of_several_selected_items_moves_all_of_them(qapp):
    """Never explicitly verified before: Qt's built-in ItemIsMovable handling
    moves every selected item together when you drag any one of them, not
    just the one under the cursor. Confirms that holds here too, since
    InstanceItem overrides mousePressEvent (for routing-mode port-picking)
    and it would be easy for that override to have broken the default
    multi-drag behavior without anyone noticing in a single-item test."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    inst_a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    item_a = scene.add_instance_item(inst_a.id)
    inst_b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=20.0)
    item_b = scene.add_instance_item(inst_b.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    item_a.setSelected(True)
    item_b.setSelected(True)
    assert item_a.isSelected() and item_b.isSelected()

    # drag item_a (under the cursor) by (+3, +2); item_b must follow by the
    # same delta even though the cursor never touches it
    press_view_pt = view.mapFromScene(QPointF(5.0, 0.0))
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, press_view_pt)
    move_view_pt = view.mapFromScene(QPointF(8.0, 2.0))
    QTest.mouseMove(view.viewport(), move_view_pt)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, move_view_pt)

    t_a = doc.get_transform(inst_a.id)
    t_b = doc.get_transform(inst_b.id)
    assert math.isclose(t_a.x, 3.0, abs_tol=1e-6) and math.isclose(t_a.y, 2.0, abs_tol=1e-6)
    assert math.isclose(t_b.x, 3.0, abs_tol=1e-6) and math.isclose(t_b.y, 22.0, abs_tol=1e-6)


def test_pressing_with_a_route_selected_does_not_crash(qapp):
    """Real crash, reported from use: routes (and the reference backdrop) are
    selectable InstanceItems whose inst_id is a route id / -1, not a key in
    document.instances. mousePressEvent snapshotted get_transform() for *every*
    selected item, so selecting a route — easy when clicking overlapping items
    in a stack — raised KeyError and aborted the press, breaking selection.
    The snapshot must skip non-instance items."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    scene.add_instance_item(a.id)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=80.0, y=0.0)
    scene.add_instance_item(b.id)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    route_item = scene.add_route_item(route.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    # Select the route the way a stacked-click would leave it, then press.
    route_item.setSelected(True)
    press_view_pt = view.mapFromScene(QPointF(40.0, 0.0))
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, press_view_pt)  # must not raise
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, press_view_pt)

    # The route id must never end up in the movable-instance snapshot.
    assert route.id not in view._drag_start_transforms


def test_middle_drag_pans_even_when_content_fits_entirely_in_viewport(qapp):
    """Real bug, reported from actual use: with no explicit scene rect,
    QGraphicsView auto-computes its scrollable range from the placed
    content's own tight bounding box. A single small waveguide easily
    fits inside a normal-sized window, so that range collapsed to exactly
    (0, 0) — middle-drag panning had nowhere to scroll to at all, even
    though the press/move/release handling itself was correct. Fixed by
    giving LayoutScene a large fixed sceneRect independent of content size."""
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(inst.id)

    view = LayoutView(scene)
    view.resize(800, 600)
    view.show()

    assert view.horizontalScrollBar().minimum() < view.horizontalScrollBar().maximum()
    assert view.verticalScrollBar().minimum() < view.verticalScrollBar().maximum()

    center_before = view.mapToScene(view.viewport().rect().center())

    start = view.viewport().rect().center()
    QTest.mousePress(view.viewport(), Qt.MiddleButton, Qt.NoModifier, start)
    end = QPointF(start.x() - 100, start.y() - 100).toPoint()
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MiddleButton, Qt.NoModifier, end)

    center_after = view.mapToScene(view.viewport().rect().center())
    assert center_before != center_after
