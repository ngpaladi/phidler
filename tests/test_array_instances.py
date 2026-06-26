"""Arraying a base component (Properties > Array), replacing the old
standalone *_array catalog entries."""

import math

import gdsfactory as gf
import klayout.db as kdb

from phidler.canvas.scene import LayoutScene
from phidler.export_script import export_python_script
from phidler.import_script import load_python_script
from phidler.main_window import _without_array_variants
from phidler.model.commands import SetArrayCommand
from phidler.model.document import LayoutDocument, Transform
from phidler.model.placed_instance import ArraySpec
from phidler.pdk_catalog import build_catalog
from phidler.project_io import load_project, save_project


def _bboxes(shapes_by_layer, transform=None):
    """A sorted list of each polygon's (xmin, ymin, xmax, ymax). When a
    transform is given, points are mapped through it first (turning local
    shapes into absolute coordinates) so canvas geometry and exported GDS can
    be compared in the same frame."""
    boxes = []
    for shapes in shapes_by_layer.values():
        for hull, _holes in shapes:
            pts = hull if transform is None else _mapped(hull, transform)
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return sorted(boxes)


def _mapped(hull, transform):
    out = []
    for x, y in hull:
        dp = transform * kdb.DPoint(x, y)
        out.append((dp.x, dp.y))
    return out


def test_canvas_tiling_matches_exported_gds_under_rotation_and_mirror(qapp, tmp_path):
    """The canvas tiles polygons itself (get_polygons_for_instance); GDS export
    uses gdsfactory's native array reference. They must agree pixel-for-pixel,
    including when the array is rotated + mirrored with distinct pitches —
    otherwise the displayed layout lies about what gets fabricated."""
    doc = LayoutDocument()
    inst = doc.add_instance(
        "straight",
        {"length": 10.0, "width": 0.5},
        array=ArraySpec(columns=3, rows=2, column_pitch=20.0, row_pitch=7.0),
    )
    doc.set_transform(inst.id, Transform(x=5.0, y=3.0, rotation=30.0, mirror=True))

    t = doc.get_transform(inst.id)
    cplx = kdb.DCplxTrans(t.mag, t.rotation, t.mirror, t.x, t.y)
    canvas_boxes = _bboxes(doc.get_polygons_for_instance(inst.id), transform=cplx)

    out = tmp_path / "array.gds"
    doc.export_gds(str(out))
    reimported = gf.import_gds(str(out))
    gds_boxes = _bboxes(
        {key: [(_to_um(poly, reimported.kcl.dbu), []) for poly in polys] for key, polys in reimported.get_polygons(by="tuple").items()}
    )

    assert len(canvas_boxes) == 6  # 3 columns × 2 rows
    # Equal up to the GDS database-unit grid (1nm); the rotated+mirrored array
    # the canvas draws is exactly what gets written to GDS.
    for cb, gb in zip(canvas_boxes, gds_boxes):
        assert all(math.isclose(c, g, abs_tol=2e-3) for c, g in zip(cb, gb)), (cb, gb)


def _to_um(poly, dbu):
    dpoly = poly.to_dtype(dbu)
    return [(p.x, p.y) for p in dpoly.each_point_hull()]


def test_arrayed_instance_renders_all_copies(qapp):
    doc = LayoutDocument()
    inst = doc.add_instance(
        "straight",
        {"length": 10.0, "width": 0.5},
        array=ArraySpec(columns=3, rows=2, column_pitch=20.0, row_pitch=7.0),
    )
    shapes = doc.get_polygons_for_instance(inst.id)
    assert sum(len(s) for s in shapes.values()) == 6  # 3 × 2 copies on the canvas


def test_single_instance_is_not_tiled(qapp):
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    assert inst.array.is_array is False
    shapes = doc.get_polygons_for_instance(inst.id)
    n_polys = sum(len(s) for s in shapes.values())
    assert n_polys == 1


def test_set_array_command_is_undoable(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(inst.id)

    cmd = SetArrayCommand(
        doc, scene, inst.id, ArraySpec(), ArraySpec(columns=2, rows=3, column_pitch=20.0, row_pitch=5.0)
    )
    cmd.redo()
    assert doc.instances[inst.id].array.is_array
    assert sum(len(s) for s in doc.get_polygons_for_instance(inst.id).values()) == 6

    cmd.undo()
    assert doc.instances[inst.id].array.is_array is False
    assert sum(len(s) for s in doc.get_polygons_for_instance(inst.id).values()) == 1


def test_array_survives_project_save_and_load(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    inst = doc.add_instance(
        "straight",
        {"length": 10.0, "width": 0.5},
        array=ArraySpec(columns=4, rows=2, column_pitch=15.0, row_pitch=6.0),
    )
    scene.add_instance_item(inst.id)
    path = tmp_path / "p.phidler"
    save_project(doc, str(path))

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_project(str(path), doc2, scene2)
    loaded = next(iter(doc2.instances.values()))
    assert loaded.array == ArraySpec(columns=4, rows=2, column_pitch=15.0, row_pitch=6.0)


def test_array_round_trips_through_python_script(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    doc.add_instance(
        "straight",
        {"length": 10.0, "width": 0.5},
        array=ArraySpec(columns=3, rows=2, column_pitch=20.0, row_pitch=7.0),
    )
    script = tmp_path / "design.py"
    export_python_script(doc, str(script))

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_python_script(str(script), doc2, scene2)
    loaded = next(iter(doc2.instances.values()))
    assert loaded.array == ArraySpec(columns=3, rows=2, column_pitch=20.0, row_pitch=7.0)


def test_palette_filter_hides_array_variants_but_keeps_base_components():
    catalog = build_catalog()
    filtered = _without_array_variants(catalog)
    names = {s.name for specs in filtered.values() for s in specs}
    assert "straight" in names
    assert "straight_array" not in names
    assert not any("array" in n.lower() for n in names)


def test_can_route_to_an_arrayed_instance_anchor_port(qapp):
    """The array wrapper exposes the anchor element's ports, so an arrayed
    instance is still a valid route endpoint (routing connects to the array
    as a unit, at element (0,0))."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    arrayed = doc.add_instance(
        "straight", {"length": 10.0, "width": 0.5}, array=ArraySpec(columns=3, rows=1, column_pitch=20.0)
    )
    plain = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=30.0)
    route = doc.add_route(arrayed.id, "o2", plain.id, "o1", "strip")
    assert route.id in doc.routes


def test_arrayed_instance_registers_its_layers(qapp):
    doc = LayoutDocument()
    inst = doc.add_instance(
        "straight", {"length": 10.0, "width": 0.5}, array=ArraySpec(columns=2, rows=2, column_pitch=20.0, row_pitch=5.0)
    )
    shape_layers = doc.get_polygons_for_instance(inst.id).keys()
    assert shape_layers  # the array contributes geometry
    assert all(key in doc.layers for key in shape_layers)  # …and its layers are tracked


def test_get_bbox_extent_reports_component_size(qapp):
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    w, h = doc.get_bbox_extent_for_instance(inst.id)
    assert math.isclose(w, 10.0, abs_tol=1e-6)
    assert math.isclose(h, 0.5, abs_tol=1e-6)
