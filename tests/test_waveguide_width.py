"""The project waveguide width: one width for every route and component drawn
in the project cross-section."""
import dataclasses

import pytest

from phidler.export_script import export_python_script
from phidler.import_script import load_python_script
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument, ProjectSettings
from phidler.panels.project_settings_dialog import ProjectSettingsDialog
from phidler.project_io import load_project, save_project


def _optical_widths(doc, inst_id):
    return sorted({round(w, 3) for *_rest, w in doc.get_ports_for_instance(inst_id)})


def _route_width(doc, route_id):
    """Width of the route's waveguide, from the y extent of a horizontal run."""
    shapes = doc.get_shapes_for_route(route_id)
    widths = []
    for polys in shapes.values():
        for hull, _holes in polys:
            xs = [p[0] for p in hull]
            ys = [p[1] for p in hull]
            widths.append(round(min(max(xs) - min(xs), max(ys) - min(ys)), 3))
    return min(widths)


def _two_straights(doc):
    a = doc.add_instance("straight", {"length": 10.0})
    b = doc.add_instance("straight", {"length": 10.0}, x=60.0)
    route = doc.add_route(a.id, "o2", b.id, "o1")
    return a, b, route


def test_default_is_the_pdk_width():
    doc = LayoutDocument()
    assert doc.project_settings.waveguide_width_um is None
    assert doc.waveguide_width_um() == pytest.approx(0.5)
    assert doc.effective_cross_section("strip") == "strip"
    a, _b, route = _two_straights(doc)
    assert _optical_widths(doc, a.id) == [0.5]
    assert _route_width(doc, route.id) == pytest.approx(0.5)


def test_set_width_rebuilds_placed_components_and_routes():
    doc = LayoutDocument()
    a, b, route = _two_straights(doc)
    coupler = doc.add_instance("coupler", {}, y=40.0)

    inst_ids, route_ids = doc.set_waveguide_width(0.8)

    assert set(inst_ids) == {a.id, b.id, coupler.id}
    assert route_ids == [route.id]
    assert doc.waveguide_width_um() == pytest.approx(0.8)
    for inst_id in (a.id, b.id, coupler.id):
        assert _optical_widths(doc, inst_id) == [0.8]
    assert _route_width(doc, route.id) == pytest.approx(0.8)
    # The recipe is unchanged: the width lives in the project, not the kwargs.
    assert doc.instances[a.id].kwargs == {"length": 10.0}
    assert doc.routes[route.id].cross_section == "strip"

    doc.set_waveguide_width(None)
    assert _optical_widths(doc, a.id) == [0.5]
    assert _route_width(doc, route.id) == pytest.approx(0.5)


def test_new_components_and_routes_use_the_project_width():
    doc = LayoutDocument()
    doc.set_waveguide_width(1.2)
    a, _b, route = _two_straights(doc)
    assert _optical_widths(doc, a.id) == [1.2]
    assert _route_width(doc, route.id) == pytest.approx(1.2)


def test_explicit_width_follows_only_when_it_was_the_waveguide_width():
    doc = LayoutDocument()
    default = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    custom = doc.add_instance("straight", {"length": 10.0, "width": 2.0}, y=10.0)

    doc.set_waveguide_width(0.8)

    assert doc.instances[default.id].kwargs["width"] == pytest.approx(0.8)
    assert _optical_widths(doc, default.id) == [0.8]
    assert doc.instances[custom.id].kwargs["width"] == 2.0
    assert _optical_widths(doc, custom.id) == [2.0]


def test_other_cross_sections_keep_their_width():
    doc = LayoutDocument()
    doc.set_waveguide_width(0.8)
    nitride = doc.add_instance("straight", {"length": 10.0, "cross_section": "nitride"})
    assert _optical_widths(doc, nitride.id) == [1.0]
    assert doc.effective_cross_section("nitride") == "nitride"


def test_component_that_cannot_take_the_width_is_kept_and_reported():
    """mzi hardcodes the PDK width inside and fails to build at another one: it
    must stay on the canvas as the PDK draws it, and be named."""
    doc = LayoutDocument()
    mzi = doc.add_instance("mzi", {})
    doc.set_waveguide_width(0.8)
    assert mzi.id in doc.instances
    assert _optical_widths(doc, mzi.id) == [0.5]
    assert "mzi" in doc.width_fallback_specs
    assert not doc.follows_project_width("mzi", {})


def test_undo_of_a_delete_comes_back_at_the_current_width():
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 10.0})
    transform = doc.get_transform(inst.id)
    removed = doc.remove_instance(inst.id)
    doc.set_waveguide_width(0.8)
    doc.restore_instance(removed, transform)
    assert _optical_widths(doc, inst.id) == [0.8]


def test_project_round_trip_keeps_the_width(qapp, tmp_path):
    win = MainWindow()
    doc = win.document
    doc.set_waveguide_width(0.8)
    a, _b, route = _two_straights(doc)
    path = tmp_path / "wide.phidler"
    save_project(doc, str(path))

    other = MainWindow()
    load_project(str(path), other.document, other.scene)
    assert other.document.project_settings.waveguide_width_um == pytest.approx(0.8)
    assert _optical_widths(other.document, a.id) == [0.8]
    assert _route_width(other.document, route.id) == pytest.approx(0.8)


def test_project_saved_before_the_setting_loads_at_the_pdk_width(qapp, tmp_path):
    import json

    win = MainWindow()
    _two_straights(win.document)
    path = tmp_path / "old.phidler"
    save_project(win.document, str(path))
    data = json.loads(path.read_text())
    del data["project_settings"]["waveguide_width_um"]
    path.write_text(json.dumps(data))

    other = MainWindow()
    load_project(str(path), other.document, other.scene)
    assert other.document.project_settings.waveguide_width_um is None
    assert other.document.waveguide_width_um() == pytest.approx(0.5)


def test_exported_script_reproduces_the_width_and_reimports(qapp, tmp_path):
    win = MainWindow()
    doc = win.document
    doc.set_waveguide_width(0.8)
    a, _b, route = _two_straights(doc)
    path = tmp_path / "wide.py"
    script = export_python_script(doc, str(path))
    assert "WAVEGUIDE = gf.get_cross_section('strip', width=0.8)" in script
    assert "cross_section=WAVEGUIDE" in script

    # The script on its own draws 0.8 µm waveguides.
    namespace = {"__name__": "exported"}
    exec(compile(script, str(path), "exec"), namespace)
    top = namespace["top"]
    assert {round(p.width, 3) for inst in top.insts for p in inst.ports} == {0.8}

    other = MainWindow()
    load_python_script(str(path), other.document, other.scene)
    assert other.document.project_settings.waveguide_width_um == pytest.approx(0.8)
    assert other.document.instances[a.id].kwargs == {"length": 10.0}
    assert _optical_widths(other.document, a.id) == [0.8]
    new_route = next(iter(other.document.routes.values()))
    assert new_route.cross_section == "strip"
    assert _route_width(other.document, new_route.id) == pytest.approx(0.8)


def test_toolbar_width_redraws_the_canvas_and_marks_dirty(qapp):
    win = MainWindow()
    a = win.document.add_instance("straight", {"length": 10.0})
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("straight", {"length": 10.0}, x=60.0)
    win.scene.add_instance_item(b.id)
    route = win.document.add_route(a.id, "o2", b.id, "o1")
    win.scene.add_route_item(route.id)
    before = win.scene.items_by_inst[a.id].boundingRect().height()

    win.waveguide_width_spin.setValue(2.0)  # valueChanged applies it

    assert win.document.project_settings.waveguide_width_um == pytest.approx(2.0)
    assert win.scene.items_by_inst[a.id].boundingRect().height() > before
    assert route.id in win.scene.route_items
    assert win.isWindowModified()

    win.waveguide_width_spin.setValue(0.0)  # "PDK"
    assert win.document.project_settings.waveguide_width_um is None
    assert _optical_widths(win.document, a.id) == [0.5]


def test_quick_straight_is_placed_at_the_project_width(qapp):
    win = MainWindow()
    win._apply_project_settings(dataclasses.replace(win.document.project_settings, waveguide_width_um=0.8))
    win._place_straight_waveguide()
    inst = next(iter(win.document.instances.values()))
    assert inst.kwargs["width"] == pytest.approx(0.8)


def test_settings_dialog_round_trips_the_width(qapp):
    dialog = ProjectSettingsDialog(ProjectSettings(waveguide_width_um=0.75))
    assert dialog.result_settings().waveguide_width_um == pytest.approx(0.75)
    dialog.waveguide_width_spin.setValue(0.0)
    assert dialog.result_settings().waveguide_width_um is None
    dialog._use_suggested_width()
    assert dialog.result_settings().waveguide_width_um == pytest.approx(dialog._suggested_width_um, abs=1e-3)


def test_sim_window_seeds_core_width_from_the_project_width(qapp):
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    win.document.set_waveguide_width(0.8)
    fdtd_win = FdtdWindow(win.document, win.view)
    assert fdtd_win.mode_core_width_spin.value() == pytest.approx(0.8)


def test_odd_nanometre_width_is_snapped_not_lost():
    """0.287 µm (what Use suggested gives on the default project) cannot be
    centred on the 1 nm grid; it is snapped to an even width rather than making every
    component fall back and every route vanish."""
    doc = LayoutDocument()
    a, _b, route = _two_straights(doc)
    doc.set_waveguide_width(0.287)
    snapped = doc.project_settings.waveguide_width_um
    assert snapped in (pytest.approx(0.286), pytest.approx(0.288))  # an even number of nm
    assert _optical_widths(doc, a.id) == [round(snapped, 3)]
    assert route.id in doc.routes and _route_width(doc, route.id) == pytest.approx(snapped)
    assert not doc.width_fallback_specs


def test_undrawable_width_is_refused_and_nothing_changes():
    doc = LayoutDocument()
    a, _b, route = _two_straights(doc)
    with pytest.raises(ValueError):
        doc.set_waveguide_width(0.0004)
    assert doc.project_settings.waveguide_width_um is None
    assert _optical_widths(doc, a.id) == [0.5]
    assert route.id in doc.routes


def test_route_failure_rolls_back_to_the_old_width(monkeypatch):
    doc = LayoutDocument()
    a, _b, route = _two_straights(doc)
    doc.set_waveguide_width(0.8)
    original = doc.add_route

    def failing(*args, **kwargs):
        if doc.project_settings.waveguide_width_um == pytest.approx(1.2):
            raise RuntimeError("no room")
        return original(*args, **kwargs)

    monkeypatch.setattr(doc, "add_route", failing)
    with pytest.raises(ValueError, match="left unchanged"):
        doc.set_waveguide_width(1.2)
    assert doc.project_settings.waveguide_width_um == pytest.approx(0.8)
    assert _optical_widths(doc, a.id) == [0.8]
    assert route.id in doc.routes and _route_width(doc, route.id) == pytest.approx(0.8)


def test_part_that_builds_but_keeps_its_own_width_is_reported():
    doc = LayoutDocument()
    taper = doc.add_instance("taper", {})
    doc.set_waveguide_width(0.8)
    assert "taper" in doc.width_fallback_specs
    assert not doc.follows_project_width("taper", {})
    assert taper.id in doc.instances


def test_script_width_line_wins_over_a_stale_header(qapp, tmp_path):
    win = MainWindow()
    win.document.set_waveguide_width(0.8)
    a, _b, _route = _two_straights(win.document)
    path = tmp_path / "edited.py"
    script = export_python_script(win.document, str(path))
    path.write_text(script.replace("width=0.8)", "width=1.0)"))

    other = MainWindow()
    load_python_script(str(path), other.document, other.scene)
    assert other.document.project_settings.waveguide_width_um == pytest.approx(1.0)
    assert _optical_widths(other.document, a.id) == [1.0]


def test_refused_width_from_the_toolbar_leaves_the_spin_at_the_old_value(qapp):
    win = MainWindow()
    win.waveguide_width_spin.setValue(0.8)
    win.waveguide_width_spin.setValue(0.0004 if win.waveguide_width_spin.decimals() > 3 else 0.001)
    assert win.document.project_settings.waveguide_width_um in (pytest.approx(0.8), pytest.approx(0.002))
    assert win.waveguide_width_spin.value() == pytest.approx(win.document.project_settings.waveguide_width_um)
