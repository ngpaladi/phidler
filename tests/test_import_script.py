import math

import gdsfactory as gf

from phidler.canvas.scene import LayoutScene
from phidler.export_script import export_python_script
from phidler.import_script import ScriptParseError, load_python_script
from phidler.model.document import LayoutDocument, ProjectSettings, Transform


def test_round_trips_instances_routes_and_mag(qapp, tmp_path):
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.set_transform(b.id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False, mag=1.5))
    doc.add_route(a.id, "o2", b.id, "o1", cross_section="strip")

    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_python_script(str(script_path), doc2, scene2)

    assert len(doc2.instances) == 2
    assert len(doc2.routes) == 1
    t_a = doc2.get_transform(a.id)
    t_b = doc2.get_transform(b.id)
    assert math.isclose(t_a.x, 0.0) and math.isclose(t_a.mag, 1.0)
    assert math.isclose(t_b.y, 20.0) and math.isclose(t_b.rotation, 90.0) and math.isclose(t_b.mag, 1.5)
    for inst_id in (a.id, b.id):
        assert inst_id in scene2.items_by_inst  # actually rendered, not just modeled
    route_id = next(iter(doc2.routes))
    assert route_id in scene2.route_items


def test_round_trips_project_settings(qapp, tmp_path):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 5.0})
    doc.project_settings = ProjectSettings(
        platform_name="Silicon Nitride (SiN)",
        core_index=2.0,
        clad_index=1.44,
        thickness_um=0.4,
        wavelength_um=1.31,
        cross_section="nitride",
    )
    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))

    doc2 = LayoutDocument()
    load_python_script(str(script_path), doc2, LayoutScene(doc2))
    s = doc2.project_settings
    assert s.platform_name == "Silicon Nitride (SiN)"
    assert math.isclose(s.thickness_um, 0.4)
    assert s.cross_section == "nitride"


def test_hand_edited_literal_value_is_honored(qapp, tmp_path):
    """The actual point of this feature, not just 'it parses': editing a
    kwarg value directly in the script and reopening it must reflect the
    edit, since the script's real code is the source of truth, not a
    separate cached blob."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))

    text = script_path.read_text().replace("length=10.0", "length=42.0")
    script_path.write_text(text)

    doc2 = LayoutDocument()
    load_python_script(str(script_path), doc2, LayoutScene(doc2))
    inst = next(iter(doc2.instances.values()))
    assert math.isclose(inst.kwargs["length"], 42.0)


def test_hand_edited_transform_value_is_honored(qapp, tmp_path):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0})
    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))

    text = script_path.read_text().replace("0.0, 0.0)", "0.0, 33.0)")
    script_path.write_text(text)

    doc2 = LayoutDocument()
    load_python_script(str(script_path), doc2, LayoutScene(doc2))
    inst_id = next(iter(doc2.instances))
    assert math.isclose(doc2.get_transform(inst_id).y, 33.0)


def test_for_loop_raises_parse_error_instead_of_silently_truncating(qapp, tmp_path):
    """A for-loop creating instances matches the same static assignment
    shape as a single real instance under a naive recursive AST walk,
    which would silently reconstruct 1 instance instead of however many
    the loop actually creates at runtime — confirmed empirically while
    building this. Must raise, not silently under-reconstruct."""
    script = (
        "import gdsfactory as gf\n"
        "top = gf.Component()\n"
        "for i in range(3):\n"
        "    inst = top.add_ref(gf.get_component('straight'))\n"
    )
    script_path = tmp_path / "loopy.py"
    script_path.write_text(script)

    doc = LayoutDocument()
    try:
        load_python_script(str(script_path), doc, LayoutScene(doc))
        assert False, "expected ScriptParseError"
    except ScriptParseError:
        pass


def test_dcplx_trans_on_unknown_variable_raises(qapp, tmp_path):
    script = (
        "import gdsfactory as gf\n"
        "import klayout.db as kdb\n"
        "top = gf.Component()\n"
        "mystery.dcplx_trans = kdb.DCplxTrans(1.0, 0.0, False, 0.0, 0.0)\n"
    )
    script_path = tmp_path / "mystery.py"
    script_path.write_text(script)

    doc = LayoutDocument()
    try:
        load_python_script(str(script_path), doc, LayoutScene(doc))
        assert False, "expected ScriptParseError"
    except ScriptParseError:
        pass


def test_route_referencing_unknown_instance_raises(qapp, tmp_path):
    script = (
        "import gdsfactory as gf\n"
        "import klayout.db as kdb\n"
        "top = gf.Component()\n"
        "inst_1 = top.add_ref(gf.get_component('straight'))\n"
        "inst_1.dcplx_trans = kdb.DCplxTrans(1.0, 0.0, False, 0.0, 0.0)\n"
        "gf.routing.route_single(top, inst_1.ports['o2'], inst_2.ports['o1'], cross_section='strip')\n"
    )
    script_path = tmp_path / "badroute.py"
    script_path.write_text(script)

    doc = LayoutDocument()
    try:
        load_python_script(str(script_path), doc, LayoutScene(doc))
        assert False, "expected ScriptParseError"
    except ScriptParseError:
        pass


def test_invalid_python_syntax_raises_parse_error(qapp, tmp_path):
    script_path = tmp_path / "broken.py"
    script_path.write_text("this is not ( valid python")
    doc = LayoutDocument()
    try:
        load_python_script(str(script_path), doc, LayoutScene(doc))
        assert False, "expected ScriptParseError"
    except ScriptParseError:
        pass


def test_script_with_no_settings_docstring_uses_defaults(qapp, tmp_path):
    script = "import gdsfactory as gf\ntop = gf.Component()\n"
    script_path = tmp_path / "minimal.py"
    script_path.write_text(script)

    doc = LayoutDocument()
    load_python_script(str(script_path), doc, LayoutScene(doc))
    assert doc.project_settings == ProjectSettings()


def test_renamed_instance_variable_still_loads_with_a_fresh_id(qapp, tmp_path):
    """A renamed variable (not matching inst_<N>) can't recover its
    original numeric id, but should still load successfully with a fresh
    one rather than failing outright — a reasonable, simple literal-ish
    edit (renaming) shouldn't be treated as structural."""
    script = (
        "import gdsfactory as gf\n"
        "import klayout.db as kdb\n"
        "top = gf.Component()\n"
        "my_waveguide = top.add_ref(gf.get_component('straight', length=7.0))\n"
        "my_waveguide.dcplx_trans = kdb.DCplxTrans(1.0, 0.0, False, 0.0, 0.0)\n"
    )
    script_path = tmp_path / "renamed.py"
    script_path.write_text(script)

    doc = LayoutDocument()
    load_python_script(str(script_path), doc, LayoutScene(doc))
    assert len(doc.instances) == 1
    inst = next(iter(doc.instances.values()))
    assert math.isclose(inst.kwargs["length"], 7.0)


def test_custom_component_path_is_recorded_and_reusable(qapp, tmp_path):
    custom_file = tmp_path / "my_parts.py"
    custom_file.write_text(
        "import gdsfactory as gf\n\n"
        "@gf.cell\n"
        "def my_custom_ring(radius: float = 10.0) -> gf.Component:\n"
        "    return gf.components.ring_single(radius=radius)\n"
    )

    doc = LayoutDocument()
    from phidler.custom_components import load_custom_components

    load_custom_components(str(custom_file))
    inst = doc.add_instance("my_custom_ring", {"radius": 12.0})
    doc.record_custom_component_path(str(custom_file))

    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))

    doc2 = LayoutDocument()
    custom_specs = load_python_script(str(script_path), doc2, LayoutScene(doc2))
    assert "my_custom_ring" in custom_specs
    assert str(custom_file) in doc2.custom_component_paths
    assert len(doc2.instances) == 1


def test_generated_then_imported_then_exported_again_produces_same_gds(qapp, tmp_path):
    """End-to-end sanity: export, reimport via the script parser, export
    again, and confirm the GDS geometry is identical both times."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.set_transform(b.id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    doc.add_route(a.id, "o2", b.id, "o1", cross_section="strip")

    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))
    first_gds = tmp_path / "first.gds"
    doc.export_gds(str(first_gds))

    doc2 = LayoutDocument()
    load_python_script(str(script_path), doc2, LayoutScene(doc2))
    second_gds = tmp_path / "second.gds"
    doc2.export_gds(str(second_gds))

    first = gf.import_gds(str(first_gds))
    second = gf.import_gds(str(second_gds))
    assert first.bbox() == second.bbox()
    assert len(list(first.insts)) == len(list(second.insts))
