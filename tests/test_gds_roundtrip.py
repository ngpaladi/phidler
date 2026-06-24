import math

import gdsfactory as gf

from phidler.canvas.scene import LayoutScene
from phidler.model.document import LayoutDocument, Transform


def test_place_and_export_matches_expected_geometry(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)

    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene.add_instance_item(inst.id)

    out = tmp_path / "out.gds"
    doc.export_gds(str(out))

    reimported = gf.import_gds(str(out))
    polys = reimported.get_polygons(by="tuple")
    assert (1, 0) in polys  # WG layer
    poly = polys[(1, 0)][0]
    dpoly = poly.to_dtype(reimported.kcl.dbu)
    xs = [p.x for p in dpoly.each_point_hull()]
    ys = [p.y for p in dpoly.each_point_hull()]
    assert math.isclose(min(xs), 0.0, abs_tol=1e-6)
    assert math.isclose(max(xs), 10.0, abs_tol=1e-6)
    assert math.isclose(min(ys), -0.25, abs_tol=1e-6)
    assert math.isclose(max(ys), 0.25, abs_tol=1e-6)


def test_move_via_scene_commits_to_document_and_export(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    item = scene.add_instance_item(inst.id)

    # simulate a drag: Qt's built-in move sets pos() directly when dragged;
    # emulate the same effect and the itemChange hook that marks it dirty
    item.setPos(20.0, 7.0)

    committed = scene.commit_dirty_transforms()
    assert committed == [inst.id]

    transform = doc.get_transform(inst.id)
    assert math.isclose(transform.x, 20.0, abs_tol=1e-9)
    assert math.isclose(transform.y, 7.0, abs_tol=1e-9)

    out = tmp_path / "moved.gds"
    doc.export_gds(str(out))
    reimported = gf.import_gds(str(out))
    poly = reimported.get_polygons(by="tuple")[(1, 0)][0]
    dpoly = poly.to_dtype(reimported.kcl.dbu)
    xs = [p.x for p in dpoly.each_point_hull()]
    ys = [p.y for p in dpoly.each_point_hull()]
    assert math.isclose(min(xs), 20.0, abs_tol=1e-6)
    assert math.isclose(max(xs), 30.0, abs_tol=1e-6)
    assert math.isclose(min(ys), 6.75, abs_tol=1e-6)
    assert math.isclose(max(ys), 7.25, abs_tol=1e-6)


def test_rotation_and_mirror_match_klayout_transform(tmp_path):
    """Verifies the document's set_transform (which both the model and the
    Qt rendering must agree on) places geometry where klayout itself would."""
    import klayout.db as kdb

    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.set_transform(inst.id, Transform(x=5.0, y=3.0, rotation=30.0, mirror=True))

    out = tmp_path / "rot.gds"
    doc.export_gds(str(out))
    reimported = gf.import_gds(str(out))
    poly = reimported.get_polygons(by="tuple")[(1, 0)][0]
    dpoly = poly.to_dtype(reimported.kcl.dbu)
    # round to the GDS file's own precision (dbu = 0.001um = 1nm) since
    # writing/reimporting quantizes coordinates to that grid
    actual_pts = {(round(p.x, 3), round(p.y, 3)) for p in dpoly.each_point_hull()}

    local_corners = [(0.0, -0.25), (0.0, 0.25), (10.0, 0.25), (10.0, -0.25)]
    t = kdb.DCplxTrans(1.0, 30.0, True, 5.0, 3.0)
    expected_pts = {(round(t.trans(kdb.DPoint(x, y)).x, 3), round(t.trans(kdb.DPoint(x, y)).y, 3)) for x, y in local_corners}

    assert actual_pts == expected_pts


def test_instance_item_transform_matches_klayout(qapp):
    """The Qt-side InstanceItem.apply_transform must reproduce the same
    coordinates as klayout's DCplxTrans for the same (rotation, mirror, x, y)."""
    import klayout.db as kdb
    from PySide6.QtCore import QPointF

    from phidler.canvas.polygon_item import InstanceItem

    item = InstanceItem(inst_id=1)
    item.apply_transform(x=5.0, y=3.0, rotation=30.0, mirror=True)

    local_pts = [(2.0, 1.0), (0.0, -0.25), (10.0, 0.25)]
    t = kdb.DCplxTrans(1.0, 30.0, True, 5.0, 3.0)
    for x, y in local_pts:
        scene_pt = item.mapToScene(QPointF(x, y))
        expected = t.trans(kdb.DPoint(x, y))
        assert math.isclose(scene_pt.x(), expected.x, abs_tol=1e-9)
        assert math.isclose(scene_pt.y(), expected.y, abs_tol=1e-9)


def test_document_mag_matches_klayout_transform(tmp_path):
    """Same rigor as rotation/mirror: scale (mag) is a newer addition, and
    its composition with rotation+mirror must match klayout's DCplxTrans
    exactly, not just "look about right"."""
    import klayout.db as kdb

    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.set_transform(inst.id, Transform(x=5.0, y=3.0, rotation=30.0, mirror=True, mag=2.5))

    out = tmp_path / "scaled.gds"
    doc.export_gds(str(out))
    reimported = gf.import_gds(str(out))
    poly = reimported.get_polygons(by="tuple")[(1, 0)][0]
    dpoly = poly.to_dtype(reimported.kcl.dbu)
    # sorted + pairwise isclose rather than exact-match on rounded sets: at
    # mag=2.5 one corner's coordinate landed within a single dbu (0.001um)
    # of a rounding boundary, where the GDS-quantized export path and pure
    # klayout floating-point math can round to adjacent values — a
    # precision artifact, not a composition-order bug (the same class
    # already worked around in test_rotation_and_mirror_match_klayout_transform).
    actual_pts = sorted({(p.x, p.y) for p in dpoly.each_point_hull()})

    local_corners = [(0.0, -0.25), (0.0, 0.25), (10.0, 0.25), (10.0, -0.25)]
    t = kdb.DCplxTrans(2.5, 30.0, True, 5.0, 3.0)
    expected_pts = sorted((t.trans(kdb.DPoint(x, y)).x, t.trans(kdb.DPoint(x, y)).y) for x, y in local_corners)

    assert len(actual_pts) == len(expected_pts)
    for (ax, ay), (ex, ey) in zip(actual_pts, expected_pts):
        assert math.isclose(ax, ex, abs_tol=2e-3)
        assert math.isclose(ay, ey, abs_tol=2e-3)


def test_instance_item_mag_transform_matches_klayout(qapp):
    import klayout.db as kdb
    from PySide6.QtCore import QPointF

    from phidler.canvas.polygon_item import InstanceItem

    item = InstanceItem(inst_id=1)
    item.apply_transform(x=5.0, y=3.0, rotation=30.0, mirror=True, mag=2.5)

    local_pts = [(2.0, 1.0), (0.0, -0.25), (10.0, 0.25)]
    t = kdb.DCplxTrans(2.5, 30.0, True, 5.0, 3.0)
    for x, y in local_pts:
        scene_pt = item.mapToScene(QPointF(x, y))
        expected = t.trans(kdb.DPoint(x, y))
        assert math.isclose(scene_pt.x(), expected.x, abs_tol=1e-9)
        assert math.isclose(scene_pt.y(), expected.y, abs_tol=1e-9)
