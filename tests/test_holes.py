import gdsfactory as gf
import klayout.db as kdb
from PySide6.QtCore import QPointF

from phidler.canvas.polygon_item import InstanceItem
from phidler.model.document import LayoutDocument


def _make_holed_component() -> gf.Component:
    c = gf.Component()
    outer = kdb.DPolygon([kdb.DPoint(0, 0), kdb.DPoint(10, 0), kdb.DPoint(10, 10), kdb.DPoint(0, 10)])
    outer.insert_hole([kdb.DPoint(3, 3), kdb.DPoint(7, 3), kdb.DPoint(7, 7), kdb.DPoint(3, 7)])
    c.shapes(c.kcl.layer(1, 0)).insert(outer.to_itype(c.kcl.dbu))
    return c


def test_document_preserves_holes_in_extracted_shapes():
    doc = LayoutDocument()
    cell = _make_holed_component()
    shapes = doc._shapes_for_cell(cell)
    hull, holes = shapes[(1, 0)][0]
    assert len(hull) == 4
    assert len(holes) == 1
    assert len(holes[0]) == 4


def test_instance_item_path_excludes_hole_interior(qapp):
    """The rendered path must NOT fill the hole — a point inside the hole
    should be outside the painter path, even though it's inside the hull."""
    cell = _make_holed_component()
    doc = LayoutDocument()
    shapes = doc._shapes_for_cell(cell)

    item = InstanceItem(inst_id=1)
    item.set_geometry(shapes, doc.layers)

    path_item = item._layer_children[(1, 0)][0]
    path = path_item.path()

    assert path.contains(QPointF(1.0, 1.0))  # inside hull, outside hole
    assert not path.contains(QPointF(5.0, 5.0))  # inside the hole
