"""The lithium niobate 1 x 2 MZI switch: one input, a Y-junction splitter."""
import os
import subprocess
import sys

import numpy as np
import pytest

from phidler import chip_circuit as cc
from phidler.model.document import LayoutDocument

pytestmark = pytest.mark.skipif(
    not (cc.AVAILABLE and hasattr(cc.lnoi, "mzi_switch_1x2")),
    reason="needs photonfdtd with lnoi.mzi_switch_1x2 (0.16.2 or newer)",
)

NAME = "lnoi_mzi_switch_1x2"
V_PI = 5.0


@pytest.fixture(autouse=True)
def _cells(monkeypatch):
    # A closed-form stand-in for the coupler-length mode solve.
    monkeypatch.setattr(cc.lnoi, "coupler_length", lambda gap, width, stack, split: 200.0 * width + 100.0 * gap)
    cc.register_cells()


@pytest.fixture
def ideal_model(monkeypatch):
    """Replace the cross-section solve with an ideal switch: V_pi = 5 V and an
    exact 50:50 output coupler. Everything after it (photonfdtd's from_lnoi,
    the chip netlist, the solver) is the real code."""
    from photonfdtd import lnoi

    def fake_model(cell, **_):
        rec = dict(cell.info["lnoi"])
        seg = rec["segments"][0]
        d = 1.55e-6 / (2 * V_PI * (seg["x1"] - seg["x0"]) * 1e-6) / 2
        kappa = np.pi / (4 * rec["coupler"]["length"] * 1e-6)
        return lnoi.SwitchModel(kind=rec["kind"], wavelength=1.55e-6, kappa=kappa,
                                slopes=np.array([[-d, 0, 0], [d, 0, 0]]), rec=rec,
                                electrode_names=["sig", "gnd_top", "gnd_bot"], signals=["sig"],
                                info=dict(n_even=1.9, n_odd=1.9, coupling_length=0.0, vpi=V_PI))

    monkeypatch.setattr(lnoi, "model", fake_model)


def _optical(doc, inst_id):
    return {name: w for name, *_xy, w in doc.get_ports_for_instance(inst_id) if name.startswith("o")}


def test_in_the_palette():
    from phidler.pdk_catalog import build_catalog

    names = {s.name for s in build_catalog().get(cc.CATEGORY, [])}
    assert NAME in names


def test_one_input_two_outputs_and_a_50_50_output_coupler():
    doc = LayoutDocument()
    sw = doc.add_instance(NAME, {"arm_length": 500.0})
    ports = _optical(doc, sw.id)
    assert set(ports) == {"o1", "o3", "o4"}
    rec = doc.instances[sw.id].cell.info["lnoi"]
    assert rec["kind"] == "mzi_switch_1x2"
    assert rec["coupler"]["length"] == pytest.approx(cc.coupler_length_50_50(1.0, 1.2))


def test_follows_the_project_width():
    doc = LayoutDocument()
    doc.set_waveguide_width(1.0, cross_section=cc.LN_CROSS_SECTION)
    sw = doc.add_instance(NAME, {"arm_length": 500.0})
    assert set(_optical(doc, sw.id).values()) == {1.0}
    assert doc.instances[sw.id].cell.info["lnoi"]["coupler"]["length"] == pytest.approx(
        cc.coupler_length_50_50(1.0, 1.0))
    doc.set_waveguide_width(1.1)
    assert set(_optical(doc, sw.id).values()) == {1.1}


def test_circuit_splits_at_0v_and_switches_at_half_vpi(qapp, ideal_model):
    """Through the Circuit tab's path: a pad on the signal, a detector on each
    output, light into the single input."""
    doc = LayoutDocument()
    sw = doc.add_instance(NAME, {"arm_length": 500.0})
    pad = doc.add_instance("pad", {}, x=-300.0, y=250.0)
    pd_up = doc.add_instance("waveguide_photodetector", {}, x=1400.0, y=60.0)
    pd_lo = doc.add_instance("waveguide_photodetector", {}, x=1400.0, y=-60.0)
    doc.add_route(pad.id, "e4", sw.id, "sig_w", cross_section="metal_routing")
    doc.add_route(sw.id, "o3", pd_up.id, "o1", cross_section=cc.LN_CROSS_SECTION)
    doc.add_route(sw.id, "o4", pd_lo.id, "o1", cross_section=cc.LN_CROSS_SECTION)

    circ = cc.build_circuit(doc, cc.CircuitConfig())
    assert len(circ.detectors) == 2
    port_in = next(p for p in circ.optical_ports if p.endswith(",o1") and f"{sw.id}" in p)
    pad_net = next(n for n in circ.nets if any("sig" in m for m in circ.net_members[n]))
    specs = {n: cc.DriveSpec("dc", 0.0) for n in circ.nets}
    sweep = cc.voltage_sweep(circ, specs, {port_in: 1.0}, 1.55, pad_net, [0.0, V_PI / 2, -V_PI / 2])
    a, b = (np.asarray(v) for v in sweep.currents.values())
    total = a + b
    assert np.allclose(total, total[0], rtol=1e-6)               # light only moves between them
    assert a[0] == pytest.approx(b[0], rel=1e-6)                 # 50:50 at 0 V
    assert max(a[1], b[1]) > 0.99 * total[1]                     # all to one at +V_pi/2
    assert max(a[2], b[2]) > 0.99 * total[2]                     # all to the other at -V_pi/2
    assert np.argmax([a[1], b[1]]) != np.argmax([a[2], b[2]])


def test_route_into_the_input_and_round_trip(qapp, tmp_path):
    from phidler.main_window import MainWindow
    from phidler.project_io import load_project, save_project

    win = MainWindow()
    doc = win.document
    doc.set_waveguide_width(1.0, cross_section=cc.LN_CROSS_SECTION)
    feed = doc.add_instance("lnoi_phase_shifter", {"length": 100.0}, x=-400.0)
    sw = doc.add_instance(NAME, {"arm_length": 500.0})
    route = doc.add_route(feed.id, "o2", sw.id, "o1", cross_section=cc.LN_CROSS_SECTION)
    assert route.refs
    path = tmp_path / "1x2.phidler"
    save_project(doc, str(path))

    other = MainWindow()
    load_project(str(path), other.document, other.scene)
    assert set(_optical(other.document, sw.id)) == {"o1", "o3", "o4"}
    assert set(_optical(other.document, sw.id).values()) == {1.0}
    assert len(other.document.routes) == 1


def test_exported_script_runs_on_its_own(tmp_path):
    from phidler.export_script import export_python_script

    doc = LayoutDocument()
    doc.add_instance(NAME, {"arm_length": 500.0})
    cc.coupler_length_50_50(1.0, 1.2)  # cached for the fresh process
    path = tmp_path / "s.py"
    export_python_script(doc, str(path))
    check = (
        f"import runpy; top = runpy.run_path({str(path)!r})['top']\n"
        "assert sorted(p.name for p in top.insts[0].ports if p.name.startswith('o')) == ['o1', 'o3', 'o4']\n"
    )
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True,
                            env={**os.environ}, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
