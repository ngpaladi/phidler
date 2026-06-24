import importlib.util
import math
import subprocess
import sys

import gdsfactory as gf

from phidler.export_script import export_python_script
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument, Transform


def _run_generated_script(path: str):
    """Executes a generated script as a real module — the only way to
    actually prove it's valid, runnable Python that reproduces the design,
    not just plausible-looking text.

    Note: importlib's exec_module() sets the module's __name__ to whatever
    name spec_from_file_location was given, never "__main__" — so this
    helper does NOT exercise the script's `if __name__ == "__main__":`
    block (the GDS-writing part). That's covered separately by actually
    invoking the script as a subprocess (see
    test_running_script_as_a_real_process_writes_matching_named_gds)."""
    spec = importlib.util.spec_from_file_location("generated_layout_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.top


def test_generated_script_runs_and_reproduces_geometry(qapp, tmp_path):
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.set_transform(b.id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    doc.add_route(a.id, "o2", b.id, "o1", cross_section="strip")

    script_path = tmp_path / "layout.py"
    export_python_script(doc, str(script_path))

    top_from_script = _run_generated_script(str(script_path))

    original_gds = tmp_path / "original.gds"
    doc.export_gds(str(original_gds))
    original = gf.import_gds(str(original_gds))

    assert top_from_script.bbox() == original.bbox()
    assert len(list(top_from_script.insts)) == len(list(original.insts))


def test_generated_script_with_no_instances_is_still_valid_python(qapp, tmp_path):
    doc = LayoutDocument()
    script_path = tmp_path / "empty.py"
    export_python_script(doc, str(script_path))

    top = _run_generated_script(str(script_path))
    assert top.bbox().empty()


def test_generated_script_preserves_kwargs(qapp, tmp_path):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 25.0})

    script_path = tmp_path / "layout.py"
    script = export_python_script(doc, str(script_path))
    assert "length=25.0" in script

    top = _run_generated_script(str(script_path))
    assert math.isclose(top.bbox().width(), 25.0, abs_tol=1e-6)


def test_generated_script_for_route_with_missing_endpoint_adds_a_comment_not_a_crash(qapp, tmp_path):
    """A route can outlive its endpoint instance (no cascade-delete is a
    known, deliberate limitation elsewhere in this app) — the script
    generator must degrade gracefully the same way, not produce a NameError
    when the script is actually run."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=20.0, rotation=90.0)
    route = doc.add_route(a.id, "o2", b.id, "o1", cross_section="strip")
    doc.remove_instance(a.id)  # orphan the route deliberately

    script_path = tmp_path / "layout.py"
    script = export_python_script(doc, str(script_path))
    assert f"route #{route.id}" in script

    top = _run_generated_script(str(script_path))  # must not raise (e.g. NameError on inst_<a>)
    assert not top.bbox().empty()


def test_generated_script_notes_custom_component_paths(qapp, tmp_path):
    doc = LayoutDocument()
    doc.custom_component_paths.append("/some/path/my_parts.py")

    script_path = tmp_path / "layout.py"
    script = export_python_script(doc, str(script_path))
    assert "load_custom_components" in script
    assert "/some/path/my_parts.py" in script


def test_export_python_script_through_main_window(qapp, tmp_path):
    win = MainWindow()
    win._place_straight_waveguide()

    script_path = tmp_path / "layout.py"
    from phidler.export_script import export_python_script as _export

    _export(win.document, str(script_path))
    top = _run_generated_script(str(script_path))
    assert not top.bbox().empty()


def test_running_script_as_a_real_process_writes_matching_named_gds(qapp, tmp_path):
    """The actual literal request: running the exported .py as a script
    (not importing it as a module — __name__ is only "__main__" when
    actually invoked this way) must write a .gds next to it with the same
    stem, e.g. running my_design.py writes my_design.gds."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})

    script_path = tmp_path / "my_design.py"
    export_python_script(doc, str(script_path))
    expected_gds = tmp_path / "my_design.gds"
    assert not expected_gds.exists()

    result = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert expected_gds.exists()

    reimported = gf.import_gds(str(expected_gds))
    assert not reimported.bbox().empty()
