"""The lithium niobate MZI switch: a 50:50 beamsplitter first, at any width,
and the LN cells following the project waveguide width."""
import subprocess
import sys

import numpy as np
import pytest

from phidler import chip_circuit as cc
from phidler.model.document import LayoutDocument

pytestmark = pytest.mark.skipif(not cc.AVAILABLE, reason=cc.UNAVAILABLE_REASON)


@pytest.fixture(autouse=True)
def _cells():
    # conftest keeps the coupler-length cache in tmp_path/cache.
    cc.register_cells()


@pytest.fixture
def fast_coupler(monkeypatch):
    """A closed-form stand-in for the mode solve: the 50:50 length grows with
    the width, as the real one does."""
    calls = []

    def fake(gap, width, stack, split):
        calls.append((gap, width))
        return 200.0 * width + 100.0 * gap

    monkeypatch.setattr(cc.lnoi, "coupler_length", fake)
    return calls


def _lnoi_doc(width=None):
    doc = LayoutDocument()
    doc.set_waveguide_width(width, cross_section=cc.LN_CROSS_SECTION)
    return doc


def _optical_widths(doc, inst_id):
    return sorted({round(w, 3) for name, *_rest, w in doc.get_ports_for_instance(inst_id) if name.startswith("o")})


def _coupler(doc, inst_id):
    return doc.instances[inst_id].cell.info["lnoi"]["coupler"]


def _split(width, gap, length):
    """Power fraction the coupler moves across, from photonfdtd's supermodes."""
    n_even, n_odd = cc.lnoi._supermodes(gap, width, cc.lnoi.X_CUT, 0.05)
    kappa = np.pi * (n_even - n_odd) / (cc.lnoi.X_CUT.wavelength * 1e-6)
    return float(np.sin(kappa * length * 1e-6) ** 2)


@pytest.mark.parametrize("width", [1.2, 1.0])
def test_first_coupler_is_a_50_50_beamsplitter(width):
    """The real mode solve: at the cell's width and at another, the drawn
    coupler splits 50:50."""
    cell = cc.lnoi_mzi_switch(arm_length=100.0, width=width)
    rec = cell.info["lnoi"]
    assert rec["width"] == pytest.approx(width)
    assert _split(width, rec["coupler"]["gap"], rec["coupler"]["length"]) == pytest.approx(0.5, abs=0.01)


def test_coupler_length_is_solved_once_and_cached_on_disk(fast_coupler):
    assert cc.coupler_length_50_50(1.0, 1.2) == pytest.approx(340.0)
    cc.coupler_length_50_50(1.0, 1.2)
    assert len(fast_coupler) == 1
    cc._coupler_lengths.clear()  # a new session reads the file
    assert cc.coupler_length_50_50(1.0, 1.2) == pytest.approx(340.0)
    assert len(fast_coupler) == 1


def test_explicit_coupler_length_is_drawn_as_given(fast_coupler):
    cell = cc.lnoi_mzi_switch(arm_length=100.0, coupler_length=276.0)
    assert cell.info["lnoi"]["coupler"]["length"] == pytest.approx(276.0)
    assert not fast_coupler


def test_switch_follows_the_project_width_and_stays_50_50(fast_coupler):
    doc = _lnoi_doc()
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    assert doc.waveguide_width_um() == pytest.approx(1.2)  # lnoi_rib's own
    assert _optical_widths(doc, sw.id) == [1.2]

    inst_ids, _ = doc.set_waveguide_width(1.0)
    assert sw.id in inst_ids
    assert _optical_widths(doc, sw.id) == [1.0]
    assert doc.instances[sw.id].kwargs == {"arm_length": 100.0}
    # The coupler was resized for the new width, not left at the old length.
    assert _coupler(doc, sw.id)["length"] == pytest.approx(cc.coupler_length_50_50(1.0, 1.0))

    later = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0}, y=200.0)
    assert _optical_widths(doc, later.id) == [1.0]


def test_width_in_the_parameters_wins(fast_coupler):
    doc = _lnoi_doc(1.0)
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0, "width": 1.4})
    assert _optical_widths(doc, sw.id) == [1.4]
    doc.set_waveguide_width(0.9)
    assert _optical_widths(doc, sw.id) == [1.4]


def test_width_applied_at_the_project_width_keeps_following(fast_coupler):
    """The properties panel sends every field back, width included; a width
    equal to the project's is not a user override."""
    doc = _lnoi_doc(1.0)
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    shown = doc.default_width_kwargs("lnoi_mzi_switch", doc.instances[sw.id].kwargs)
    assert shown["width"] == pytest.approx(1.0)
    doc.update_instance_params(sw.id, {**shown, "arm_length": 200.0})
    doc.set_waveguide_width(1.1)
    assert _optical_widths(doc, sw.id) == [1.1]


def test_other_project_cross_section_leaves_the_ln_cells_alone(fast_coupler):
    doc = LayoutDocument()
    doc.set_waveguide_width(0.8)  # strip
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    ps = doc.add_instance("lnoi_phase_shifter", {"length": 100.0}, y=200.0)
    assert _optical_widths(doc, sw.id) == [1.2]
    assert _optical_widths(doc, ps.id) == [1.2]
    assert "width" not in doc.default_width_kwargs("lnoi_mzi_switch", {})


def test_phase_shifter_follows_but_the_dbeta_switch_keeps_its_design_width(fast_coupler):
    doc = _lnoi_doc(1.0)
    ps = doc.add_instance("lnoi_phase_shifter", {"length": 100.0})
    db = doc.add_instance("lnoi_dbeta_coupler_switch", {"length": 300.0}, y=200.0)
    assert _optical_widths(doc, ps.id) == [1.0]
    assert _optical_widths(doc, db.id) == [1.2]


def test_width_applied_under_strip_moves_when_the_project_goes_to_lnoi_rib(fast_coupler):
    """Apply in the properties panel pins width=1.2 (the cell's own) while the
    project is still in strip (0.5 µm); switching to lnoi_rib still moves it."""
    doc = LayoutDocument()
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    doc.update_instance_params(sw.id, {"arm_length": 200.0, "width": 1.2})
    doc.set_waveguide_width(1.0, cross_section=cc.LN_CROSS_SECTION)
    assert _optical_widths(doc, sw.id) == [1.0]


def test_ln_preset_in_the_dialog_reaches_the_switch(qapp, fast_coupler):
    from phidler.main_window import MainWindow
    from phidler.panels.project_settings_dialog import ProjectSettingsDialog

    win = MainWindow()
    dialog = ProjectSettingsDialog(win.document.project_settings)
    dialog.platform_combo.setCurrentText("Lithium Niobate (LN)")
    assert dialog.cross_section_combo.currentText() == cc.LN_CROSS_SECTION
    dialog.waveguide_width_spin.setValue(1.0)
    settings = dialog.result_settings()
    win.document.set_waveguide_width(settings.waveguide_width_um, cross_section=settings.cross_section)
    sw = win.document.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    assert _optical_widths(win.document, sw.id) == [1.0]


def test_project_round_trip_and_a_route_off_the_switch(qapp, tmp_path, fast_coupler):
    from phidler.main_window import MainWindow
    from phidler.project_io import load_project, save_project

    win = MainWindow()
    doc = win.document
    doc.set_waveguide_width(1.0, cross_section=cc.LN_CROSS_SECTION)
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    ps = doc.add_instance("lnoi_phase_shifter", {"length": 100.0}, x=1500.0, y=12.5)
    route = doc.add_route(sw.id, "o3", ps.id, "o1", cross_section=cc.LN_CROSS_SECTION)
    assert route.refs
    path = tmp_path / "ln.phidler"
    save_project(doc, str(path))

    other = MainWindow()
    load_project(str(path), other.document, other.scene)
    assert other.document.project_settings.cross_section == cc.LN_CROSS_SECTION
    assert _optical_widths(other.document, sw.id) == [1.0]
    assert _coupler(other.document, sw.id)["length"] == pytest.approx(cc.coupler_length_50_50(1.0, 1.0))
    assert len(other.document.routes) == 1


def test_exported_script_runs_on_its_own_and_reimports(qapp, tmp_path, fast_coupler):
    from phidler.export_script import export_python_script
    from phidler.import_script import load_python_script
    from phidler.main_window import MainWindow

    doc = _lnoi_doc(1.0)
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 100.0})
    length = _coupler(doc, sw.id)["length"]
    path = tmp_path / "switch.py"
    script = export_python_script(doc, str(path))
    assert "register_cells()" in script
    assert "width=1.0" in script

    # A fresh interpreter, with the coupler length already cached (the
    # fixture's cache directory) so it doesn't run the mode solve.
    check = (
        f"import runpy; top = runpy.run_path({str(path)!r})['top']\n"
        "ws = {round(p.width, 3) for i in top.insts for p in i.ports if p.name.startswith('o')}\n"
        "assert ws == {1.0}, ws\n"
        f"assert abs(top.insts[0].cell.info['lnoi']['coupler']['length'] - {length}) < 1e-9\n"
    )
    import os

    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")}
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, env=env, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]

    other = MainWindow()
    load_python_script(str(path), other.document, other.scene)
    assert other.document.project_settings.cross_section == cc.LN_CROSS_SECTION
    new = next(iter(other.document.instances.values()))
    assert _optical_widths(other.document, new.id) == [1.0]
