from phidler.drc import run_drc
from phidler.model.document import LayoutDocument


def test_no_violations_on_default_geometry(qapp):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    violations = run_drc(doc, (1, 0), min_width=0.2, min_spacing=0.0)
    assert violations == []


def test_width_violation_detected_for_thin_polygon(qapp):
    doc = LayoutDocument()
    sliver = doc.top.add_polygon([(0, 0), (10, 0), (10, 0.05), (0, 0.05)], layer=(1, 0))
    assert sliver is not None
    violations = run_drc(doc, (1, 0), min_width=0.2, min_spacing=0.0)
    assert any(v.kind == "width" for v in violations)


def test_spacing_violation_detected_for_close_polygons(qapp):
    doc = LayoutDocument()
    doc.top.add_polygon([(0, 0), (10, 0), (10, 1), (0, 1)], layer=(1, 0))
    doc.top.add_polygon([(10.05, 0), (20, 0), (20, 1), (10.05, 1)], layer=(1, 0))
    violations = run_drc(doc, (1, 0), min_width=0.0, min_spacing=0.2)
    assert any(v.kind == "spacing" for v in violations)


def test_zero_threshold_skips_that_check(qapp):
    doc = LayoutDocument()
    doc.top.add_polygon([(0, 0), (10, 0), (10, 0.05), (0, 0.05)], layer=(1, 0))
    violations = run_drc(doc, (1, 0), min_width=0.0, min_spacing=0.0)
    assert violations == []
