"""Direct all-angle (diagonal) routing — routes take the short diagonal path
instead of a manhattan L/U-turn."""

import math

from phidler.canvas.scene import LayoutScene
from phidler.model.document import LayoutDocument
from phidler.project_io import load_project, save_project


def _two_offset_straights(doc):
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=80.0, y=40.0)
    return a, b


def _len_um(doc, route):
    return route.length * doc.top.kcl.dbu


def test_diagonal_route_is_shorter_than_manhattan(qapp):
    doc = LayoutDocument()
    a, b = _two_offset_straights(doc)
    m = doc.add_route(a.id, "o2", b.id, "o1", "strip", diagonal=False)
    manhattan_len = _len_um(doc, m)
    doc.remove_route(m.id)

    d = doc.add_route(a.id, "o2", b.id, "o1", "strip", diagonal=True)
    assert d.diagonal is True
    assert _len_um(doc, d) < manhattan_len  # the diagonal takes the short path


def test_diagonal_route_renders_exports_and_removes(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a, b = _two_offset_straights(doc)
    scene.add_instance_item(a.id)
    scene.add_instance_item(b.id)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", diagonal=True)
    scene.add_route_item(route.id)

    # The all-angle geometry is flattened to a single ordinary ref, so it
    # renders, exports, and removes through the normal route path.
    shapes = doc.get_shapes_for_route(route.id)
    assert sum(len(s) for s in shapes.values()) >= 1
    doc.export_gds(str(tmp_path / "d.gds"))
    assert (tmp_path / "d.gds").exists()
    doc.remove_route(route.id)
    assert route.id not in doc.routes


def test_diagonal_falls_back_to_manhattan_when_unrealizable(qapp):
    """Ports too close for the bend radius can't be all-angle routed; the route
    must still be created (manhattan fallback), not raise."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=2.0, y=1.0, rotation=90.0)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", diagonal=True)
    assert route.id in doc.routes  # created via fallback, no exception


def test_diagonal_survives_project_save_and_load(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a, b = _two_offset_straights(doc)
    scene.add_instance_item(a.id)
    scene.add_instance_item(b.id)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", diagonal=True)
    scene.add_route_item(route.id)
    original_len = _len_um(doc, route)

    path = tmp_path / "p.phidler"
    save_project(doc, str(path))
    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_project(str(path), doc2, scene2)
    reloaded = next(iter(doc2.routes.values()))
    assert reloaded.diagonal is True
    assert math.isclose(_len_um(doc2, reloaded), original_len, abs_tol=1.0)


def test_diagonal_round_trips_through_python_script(qapp, tmp_path):
    from phidler.export_script import export_python_script
    from phidler.import_script import load_python_script

    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a, b = _two_offset_straights(doc)
    scene.add_instance_item(a.id)
    scene.add_instance_item(b.id)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", diagonal=True)
    scene.add_route_item(route.id)

    script = tmp_path / "design.py"
    text = export_python_script(doc, str(script))
    assert "route_bundle_all_angle" in text

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_python_script(str(script), doc2, scene2)
    reloaded = next(iter(doc2.routes.values()))
    assert reloaded.diagonal is True
