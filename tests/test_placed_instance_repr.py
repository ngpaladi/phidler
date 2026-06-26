from phidler.model.document import LayoutDocument


def test_placed_instance_repr_excludes_heavy_cell_and_ref_fields(qapp):
    """cell/ref recursively repr the entire underlying KCell — harmless in
    code, but unreadable noise the moment a PlacedInstance is returned from
    the scripting console and auto-echoed (confirmed while capturing a
    console screenshot for the README: place(...) without assigning the
    result dumped a multi-thousand-character wall of text)."""
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0})
    text = repr(inst)
    assert "component_spec='straight'" in text
    assert "kwargs={'length': 10.0}" in text
    assert "cell=" not in text
    assert "ref=" not in text
    assert len(text) < 200


def test_placed_route_repr_excludes_heavy_refs_field(qapp):
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0})
    b = doc.add_instance("straight", {"length": 10.0}, x=0.0, y=20.0, rotation=90.0)
    route = doc.add_route(a.id, "o2", b.id, "o1")
    text = repr(route)
    assert "cross_section='strip'" in text
    assert "refs=" not in text  # the heavy field stays out of the repr
    assert len(text) < 300  # scalar metadata (goal/auto/amplitude/diagonal) is fine; the refs dump isn't
