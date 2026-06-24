import math

from phidler.custom_components import load_custom_components
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument

_GOOD_MODULE = '''
import gdsfactory as gf
from gdsfactory.components import straight  # imported, not defined here — must be excluded

@gf.cell
def my_decorated_part(length: float = 5.0) -> gf.Component:
    return gf.components.straight(length=length)

def my_plain_part(width: float = 0.3) -> gf.Component:
    return gf.components.straight(width=width)

def needs_an_argument(length) -> gf.Component:
    return gf.components.straight(length=length)

def raises_when_called() -> gf.Component:
    raise RuntimeError("deliberately broken")

def returns_wrong_type() -> int:
    return 42

_private_helper = lambda: gf.components.straight()
'''


def test_load_custom_components_finds_only_valid_defined_factories(qapp, tmp_path):
    path = tmp_path / "custom.py"
    path.write_text(_GOOD_MODULE)

    result = load_custom_components(str(path))

    assert set(result.specs.keys()) == {"my_decorated_part", "my_plain_part"}
    assert "straight" not in result.specs  # imported, not defined here
    assert "_private_helper" not in result.specs  # leading underscore
    assert set(result.skipped) == {"needs_an_argument", "raises_when_called", "returns_wrong_type"}


def test_loaded_custom_component_is_placeable_via_document(qapp, tmp_path):
    path = tmp_path / "custom.py"
    path.write_text(_GOOD_MODULE)
    result = load_custom_components(str(path))

    doc = LayoutDocument()
    inst = doc.add_instance("my_decorated_part", {"length": 8.0})
    shapes = doc.get_polygons_for_instance(inst.id)
    xs = [x for hull, _holes in next(iter(shapes.values())) for x, _ in hull]
    assert math.isclose(max(xs), 8.0, abs_tol=1e-6)
    assert "my_decorated_part" in result.specs  # sanity: came from the loaded module


def test_loaded_custom_component_survives_gds_export_round_trip(qapp, tmp_path):
    import gdsfactory as gf

    path = tmp_path / "custom.py"
    path.write_text(_GOOD_MODULE)
    load_custom_components(str(path))

    doc = LayoutDocument()
    doc.add_instance("my_plain_part", {"width": 0.6})
    out = tmp_path / "out.gds"
    doc.export_gds(str(out))
    reimported = gf.import_gds(str(out))
    assert not reimported.bbox().empty()


def test_import_custom_components_through_main_window_adds_to_palette(qapp, tmp_path):
    """Exercises _apply_custom_components_file — the load+merge logic split
    out of _import_custom_components specifically so it's testable without
    the blocking QFileDialog call (the dialog itself is untested here, same
    as every other File menu action in this app)."""
    path = tmp_path / "custom.py"
    path.write_text(_GOOD_MODULE)

    win = MainWindow()
    win._apply_custom_components_file(str(path))

    assert "my_decorated_part" in win.catalog_by_name
    assert "my_plain_part" in win.catalog_by_name
    assert any(s.name == "my_decorated_part" for s in win.catalog["custom"])

    win._on_placement_requested("my_decorated_part", 0.0, 0.0)
    assert len(win.document.instances) == 1

    custom_top_level = next(
        win.palette.tree.topLevelItem(i)
        for i in range(win.palette.tree.topLevelItemCount())
        if win.palette.tree.topLevelItem(i).text(0).startswith("Custom")
    )
    pretty_labels = [custom_top_level.child(i).text(0) for i in range(custom_top_level.childCount())]
    assert "My Decorated Part" in pretty_labels  # the prettify/curation work applies to custom parts too


def test_invalid_python_file_raises_clean_error(qapp, tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("this is not valid python (((")

    try:
        load_custom_components(str(path))
        assert False, "expected an error"
    except ValueError:
        pass


def test_custom_component_survives_save_reopen_in_a_fresh_session(qapp, tmp_path):
    """Real gap caught by review: a saved project stores component_spec as
    a bare string name, resolved on load via gf.get_component(name) ->
    pdk.cells lookup. Custom cells only exist in that registry because
    load_custom_components() ran *this process* — a fresh session (or just
    reopening the project tomorrow without re-importing the file first)
    would have no such entry, and load_project used to crash mid-replay
    with the document already cleared, losing everything.

    pdk.remove_cell() simulates "fresh process" without actually starting
    one: it unregisters the custom name the same way a new interpreter
    would simply never have registered it.
    """
    import gdsfactory as gf

    from phidler.project_io import load_project, save_project

    path = tmp_path / "custom.py"
    path.write_text(_GOOD_MODULE)

    win = MainWindow()
    win._apply_custom_components_file(str(path))
    win._on_placement_requested("my_decorated_part", 3.0, 4.0)
    assert len(win.document.instances) == 1

    project_path = str(tmp_path / "project.phidler")
    save_project(win.document, project_path)

    pdk = gf.get_active_pdk()
    pdk.remove_cell("my_decorated_part")
    pdk.remove_cell("my_plain_part")
    assert "my_decorated_part" not in pdk.cells

    win2 = MainWindow()
    custom_specs = load_project(project_path, win2.document, win2.scene)  # must not raise

    assert len(win2.document.instances) == 1  # the real bug: this used to be 0
    inst_id = next(iter(win2.document.instances))
    t = win2.document.get_transform(inst_id)
    assert math.isclose(t.x, 3.0, abs_tol=1e-6) and math.isclose(t.y, 4.0, abs_tol=1e-6)
    assert "my_decorated_part" in custom_specs
    assert win2.document.custom_component_paths == [str(path)]

    # and the round-trip is stable: saving again still records the path
    save_project(win2.document, str(tmp_path / "project2.phidler"))
