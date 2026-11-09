"""Whole-chip circuit simulation (phidler.chip_circuit + the Circuit tab)."""
import numpy as np
import pytest

from phidler import chip_circuit as cc
from phidler.model.document import LayoutDocument

pytestmark = pytest.mark.skipif(not cc.AVAILABLE, reason=cc.UNAVAILABLE_REASON)

V_PI = 5.0


@pytest.fixture
def fast_lnoi(monkeypatch):
    """Replace the minute-long cross-section solve with an ideal MZI switch
    (V_pi = 5 V, 50:50 couplers)."""
    from photonfdtd import lnoi

    def fake_model(cell, **_):
        rec = dict(cell.info["lnoi"])
        lam = 1.55e-6
        seg = rec["segments"][0]
        L = (seg["x1"] - seg["x0"]) * 1e-6
        d = lam / (2 * V_PI * L) / 2
        kappa = np.pi / (4 * rec["coupler"]["length"] * 1e-6)
        return lnoi.SwitchModel(kind=rec["kind"], wavelength=lam, kappa=kappa,
                                slopes=np.array([[-d, 0, 0], [d, 0, 0]]), rec=rec,
                                electrode_names=["sig", "gnd_top", "gnd_bot"], signals=["sig"],
                                info=dict(n_even=1.9, n_odd=1.9, coupling_length=0.0, vpi=V_PI))
    monkeypatch.setattr(lnoi, "model", fake_model)


def _chip_document():
    """An LN MZI switch with its signal on a pad and a photodetector on each
    output, wired with Phidler's own routing."""
    cc.register_cells()
    doc = LayoutDocument()
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 500.0})
    pad = doc.add_instance("pad", {}, x=-300.0, y=250.0)
    gnd = doc.add_instance("pad", {}, x=-300.0, y=-250.0)
    pd_up = doc.add_instance("waveguide_photodetector", {}, x=1700.0, y=60.0)
    pd_lo = doc.add_instance("waveguide_photodetector", {}, x=1700.0, y=-60.0)
    doc.add_route(pad.id, "e4", sw.id, "sig_w", cross_section="metal_routing")
    doc.add_route(sw.id, "o3", pd_up.id, "o1", cross_section=cc.LN_CROSS_SECTION)
    doc.add_route(sw.id, "o4", pd_lo.id, "o1", cross_section=cc.LN_CROSS_SECTION)
    return doc, dict(sw=sw, pad=pad, gnd=gnd, pd_up=pd_up, pd_lo=pd_lo)


def test_cells_are_in_the_palette_catalog(qapp):
    from phidler.pdk_catalog import build_catalog

    cc.register_cells()
    names = {s.name for s in build_catalog().get(cc.CATEGORY, [])}
    assert {"lnoi_mzi_switch", "lnoi_dbeta_coupler_switch", "waveguide_photodetector"} <= names


def test_layout_becomes_a_circuit_that_switches(qapp, fast_lnoi):
    doc, ids = _chip_document()
    circ = cc.build_circuit(doc, cc.CircuitConfig())
    assert len(circ.detectors) == 2
    labels = {circ.label(d) for d in circ.detectors}
    assert labels == {f"#{ids['pd_up'].id} waveguide_photodetector",
                      f"#{ids['pd_lo'].id} waveguide_photodetector"}
    # The switch's free input is an open optical port; light goes in there.
    port_in = next(p for p in circ.optical_ports if p.endswith(",o1"))
    pad_net = next(n for n in circ.nets if any("sig" in m for m in circ.net_members[n]))
    specs = {n: cc.DriveSpec("dc", 0.0) for n in circ.nets}
    sweep = cc.voltage_sweep(circ, specs, {port_in: 1.0}, 1.55, pad_net, [0.0, V_PI])
    a, b = sweep.currents.values()
    # Responsivity 0.8 A/W, 1 mW in: the light moves from one detector to the other.
    assert max(a[0], b[0]) > 0.7 and min(a[0], b[0]) < 0.01
    assert max(a[1], b[1]) > 0.7 and np.argmax([a[0], b[0]]) != np.argmax([a[1], b[1]])
    t = np.linspace(0, 4e-9, 200)
    specs[pad_net] = cc.DriveSpec("square", 0.0, V_PI, 0.5e9)
    tr = cc.time_domain(circ, specs, {port_in: 1.0}, 1.55, t)
    tot = sum(tr.currents.values())
    assert np.allclose(tot, tot[0], rtol=1e-6)        # light only moves between them


def test_circuit_tab_builds_and_runs(qapp, fast_lnoi):
    from PySide6.QtWidgets import QApplication

    from phidler.panels.circuit_tab import CircuitTab

    doc, _ = _chip_document()
    tab = CircuitTab(doc)

    def pump(cond, timeout=60.0):
        import time
        t0 = time.time()
        while not cond() and time.time() - t0 < timeout:
            QApplication.processEvents()
            time.sleep(0.01)
        return cond()

    tab._on_build()
    assert pump(lambda: not tab.is_busy()) and tab.circuit is not None, tab.status_label.text()
    assert tab.components_table.rowCount() == len(tab.circuit.chip.models)
    pad_net = next(n for n in tab.circuit.nets if any("sig" in m for m in tab.circuit.net_members[n]))
    tab.set_drive(pad_net, "dc", V_PI)
    tab.analysis_combo.setCurrentIndex(tab.analysis_combo.findData("op"))
    tab._on_run()
    assert pump(lambda: not tab.is_busy() and tab.result is not None), tab.status_label.text()
    assert tab.results_table.rowCount() > 0
    tab.analysis_combo.setCurrentIndex(tab.analysis_combo.findData("vsweep"))
    tab.sweep_net_combo.setCurrentText(pad_net)
    tab.sweep_points.setValue(11)
    tab.result = None
    tab._on_run()
    assert pump(lambda: not tab.is_busy() and tab.result is not None), tab.status_label.text()
    assert len(tab.current_plot.series) == 2
    assert not tab.current_plot.grab().isNull()
    tab.shutdown()
