"""Routes re-route when a connected component moves (drag, rotate, flip — all
go through MoveInstanceCommand), so the track follows its endpoints."""

from PySide6.QtGui import QUndoStack

from phidler.canvas.scene import LayoutScene
from phidler.model.commands import MoveInstanceCommand
from phidler.model.document import LayoutDocument, Transform


def _setup():
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    undo = QUndoStack()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    scene.add_instance_item(a.id)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=80.0, y=0.0)
    scene.add_instance_item(b.id)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    scene.add_route_item(route.id)
    return doc, scene, undo, a, b, route


def _route_max_x(doc, route_id):
    return max(x for shapes in doc.get_shapes_for_route(route_id).values() for hull, _ in shapes for x, _y in hull)


def test_route_follows_a_moved_endpoint(qapp):
    doc, scene, undo, a, b, route = _setup()
    assert _route_max_x(doc, route.id) < 90  # reaches b at x≈80

    undo.push(MoveInstanceCommand(doc, scene, b.id, doc.get_transform(b.id), Transform(x=180.0, y=0.0, rotation=0.0, mirror=False)))
    assert _route_max_x(doc, route.id) > 170  # the track followed b to x≈180
    assert route.id in scene.route_items  # scene item refreshed, not orphaned


def test_route_reroute_is_undoable(qapp):
    doc, scene, undo, a, b, route = _setup()
    original = _route_max_x(doc, route.id)

    undo.push(MoveInstanceCommand(doc, scene, b.id, doc.get_transform(b.id), Transform(x=180.0, y=0.0, rotation=0.0, mirror=False)))
    assert _route_max_x(doc, route.id) > 170
    undo.undo()
    assert abs(_route_max_x(doc, route.id) - original) < 1.0  # back where it started


def test_unrelated_move_leaves_routes_alone(qapp):
    doc, scene, undo, a, b, route = _setup()
    before = _route_max_x(doc, route.id)
    # an instance with no routes attached
    c = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=50.0)
    scene.add_instance_item(c.id)

    undo.push(MoveInstanceCommand(doc, scene, c.id, doc.get_transform(c.id), Transform(x=40.0, y=50.0, rotation=0.0, mirror=False)))
    assert abs(_route_max_x(doc, route.id) - before) < 1.0  # untouched


def test_connected_routes_fade_during_drag_and_restore_after(qapp):
    """While a component is dragged its attached track is stale (it only
    re-routes on drop), so it's faded to signal 'will update' — not frozen —
    then restored when the drag ends."""
    from phidler.main_window import MainWindow

    win = MainWindow()
    a = win.document.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("straight", {"length": 10.0, "width": 0.5}, x=80.0, y=0.0)
    win.scene.add_instance_item(b.id)
    route = win.document.add_route(a.id, "o2", b.id, "o1", "strip")
    win.scene.add_route_item(route.id)
    assert win.scene.route_items[route.id].opacity() == 1.0

    win.view._drag_start_transforms = {b.id: win.document.get_transform(b.id)}
    win.view._dim_connected_routes()
    assert win.scene.route_items[route.id].opacity() < 1.0  # faded mid-drag
    assert route.id in win.view._dimmed_route_ids

    win.view._restore_dimmed_routes()
    assert win.scene.route_items[route.id].opacity() == 1.0  # restored on drop
    assert not win.view._dimmed_route_ids
