"""Component-by-component FDTD (phidler.component_sim): each distinct passive
part is measured alone and the circuit joins the S-matrices."""
from types import SimpleNamespace

import numpy as np
import pytest

from phidler import chip_circuit as cc
from phidler import component_sim as cs
from phidler.model.document import EtchLayer, LayoutDocument

pytestmark = pytest.mark.skipif(not cc.AVAILABLE, reason=cc.UNAVAILABLE_REASON)

# Coarse and single-wavelength: these prove the measurement, not a design.
_FAST = cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(cs, "_memory_cache", {})


def _cell(name, **kwargs):
    import gdsfactory as gf

    return gf.get_component(name, **kwargs)


# -- geometry ---------------------------------------------------------------


def test_facet_intervals_of_a_notched_polygon():
    # A "U": the line y = 1.5 is inside it twice.
    u = np.array([(0, 0), (3, 0), (3, 2), (2, 2), (2, 1), (1, 1), (1, 2), (0, 2)], float)
    assert cs._facet_intervals(u, 1, 1.5) == [(0.0, 1.0), (2.0, 3.0)]
    assert cs._facet_intervals(u, 1, 0.5) == [(0.0, 3.0)]
    assert cs._facet_intervals(u, 1, 5.0) == []


def test_ports_are_read_with_the_side_they_face(qapp):
    ports = {p.name: p for p in cs.cell_ports(_cell("bend_euler", radius=5.0))}
    assert (ports["o1"].axis, ports["o1"].sign) == (0, -1)   # faces -x
    assert (ports["o2"].axis, ports["o2"].sign) == (1, 1)    # faces +y
    assert ports["o1"].width_um == pytest.approx(0.5)


def test_a_port_off_the_axes_is_refused(qapp):
    with pytest.raises(cs.UnsupportedComponent, match="x or y"):
        cs.cell_ports(_cell("bend_euler", radius=5.0, angle=45))


def test_stubs_continue_every_simulated_layer_at_the_port(qapp):
    doc = LayoutDocument()
    cell = _cell("straight", length=3.0, cross_section="rib")
    ports = cs.cell_ports(cell)
    core_only = cs.port_stubs(cell, ports, doc.project_settings, 2.0, 5.0)
    assert sorted(layer for layer, _ in core_only) == [(1, 0), (1, 0)]
    doc.project_settings.etch_layers = (EtchLayer(3, 0, 0.09),)
    both = cs.port_stubs(cell, ports, doc.project_settings, 2.0, 5.0)
    assert sorted(layer for layer, _ in both) == [(1, 0), (1, 0), (3, 0), (3, 0)]
    # The core stub at o2 (x = 3, facing +x) is the core's width, 2 um long.
    rect = next(r for layer, r in both if layer == (1, 0) and max(x for x, _ in r) > 3.0)
    xs, ys = [x for x, _ in rect], [y for _, y in rect]
    assert max(xs) == pytest.approx(5.0) and min(xs) == pytest.approx(3.0, abs=0.01)
    assert (min(ys), max(ys)) == pytest.approx((-0.25, 0.25))


def test_the_key_follows_the_shape_not_the_cell_name(qapp):
    doc = LayoutDocument()
    st = doc.project_settings
    a, b = _cell("bend_euler", radius=10.0), _cell("bend_euler", radius=None)
    assert a.name != b.name

    def key(cell, cfg=_FAST):
        return cs.geometry_key(cell, cs.cell_ports(cell), st, cfg)

    assert key(a) == key(b)
    assert key(a) != key(_cell("bend_euler", radius=6.0))
    assert key(a) != key(a, cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.05))
    # Settings that do not change the physics do not change the key.
    assert key(a) == key(a, cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07,
                                                  max_cells=5, use_cache=False))
    # A different layer stack is a different measurement.
    k0 = key(a)
    st.core_index += 0.1
    assert key(a) != k0


# -- one component ----------------------------------------------------------


def test_a_straight_transmits_everything_with_the_phase_of_its_length(qapp):
    doc = LayoutDocument()
    cfg = cs.ComponentSimConfig(n_wavelengths=3, span_um=0.04, cell_size_um=0.07)
    short = cs.simulate_component(_cell("straight", length=2.0), doc.project_settings, cfg)
    long = cs.simulate_component(_cell("straight", length=3.0), doc.project_settings, cfg)
    assert short.ports == ("o1", "o2") and len(short.wavelengths_um) == 3
    assert not short.unconverged
    for sp in (short, long):
        assert np.abs(sp.s[("o1", "o2")]) ** 2 == pytest.approx(1.0, abs=0.02)
        assert np.abs(sp.s[("o1", "o1")]) ** 2 == pytest.approx(0.0, abs=0.01)
        assert sp.power_out("o1") == pytest.approx(1.0, abs=0.02)
        assert np.allclose(sp.s[("o1", "o2")], sp.s[("o2", "o1")])
    # One more micrometre is exp(-1j * beta * 1 um), in photonfdtd.circuit's
    # sign; the grid carries the mode a few percent slower than the solver.
    mid = 1
    step = np.angle(long.s[("o1", "o2")][mid] / short.s[("o1", "o2")][mid])
    beta_um = -step % (2 * np.pi)
    expected = (2 * np.pi * short.n_eff["o1"] / cfg.wavelength_um) % (2 * np.pi)
    turns = 2 * np.pi * short.n_eff["o1"] / cfg.wavelength_um
    assert abs((beta_um - expected + np.pi) % (2 * np.pi) - np.pi) < 0.05 * turns
    # ... and the phase runs the right way with wavelength (shorter: more phase).
    slope = np.unwrap(np.angle(long.s[("o1", "o2")]))
    assert slope[0] < slope[-1]


def test_a_bend_joins_ports_on_different_sides(qapp):
    doc = LayoutDocument()
    sp = cs.simulate_component(_cell("bend_euler", radius=4.0), doc.project_settings, _FAST)
    t = np.abs(sp.s[("o1", "o2")][0]) ** 2
    assert 0.7 < t <= 1.01
    assert np.abs(sp.s[("o1", "o1")][0]) ** 2 < 0.02
    assert sp.cells == int(np.prod(sp.grid_shape)) and sp.memory_gb > 0


def test_a_splitter_divides_the_light_evenly(qapp):
    doc = LayoutDocument()
    cell = _cell("mmi1x2", length_taper=3.0, length_mmi=5.5)
    sp = cs.simulate_component(cell, doc.project_settings,
                               cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.06))
    up, down = np.abs(sp.s[("o1", "o2")][0]) ** 2, np.abs(sp.s[("o1", "o3")][0]) ** 2
    assert up == pytest.approx(down, rel=0.1)
    assert 0.25 < up < 0.5
    # From one output only about half comes back out of the input.
    assert sp.power_out("o2")[0] < 0.75


def test_results_round_trip_through_the_cache(qapp, tmp_path):
    doc = LayoutDocument()
    sp = cs.simulate_component(_cell("straight", length=2.0), doc.project_settings, _FAST)
    cs.cache_put(sp)
    assert (cs.cache_dir() / f"{sp.key}.json").exists()
    cs._memory_cache.clear()
    back = cs.cache_get(sp.key)
    assert back.ports == sp.ports and back.grid_shape == sp.grid_shape
    assert np.allclose(back.s[("o1", "o2")], sp.s[("o1", "o2")])
    wl = np.array([1.55])
    assert np.allclose(back.model().sdict(wl)[("o1", "o2")], sp.s[("o1", "o2")])
    cs.clear_cache()
    assert cs.cache_get(sp.key) is None


# -- a layout ---------------------------------------------------------------


def _bent_route_document():
    """Two straights joined by a route with two bends."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 2.0})
    b = doc.add_instance("straight", {"length": 2.0}, x=30.0, y=25.0)
    doc.add_route(a.id, "o2", b.id, "o1")
    return doc


def test_a_layout_is_measured_one_distinct_component_at_a_time(qapp, monkeypatch):
    doc = _bent_route_document()
    runs = []
    real = cs.simulate_component
    monkeypatch.setattr(cs, "simulate_component",
                        lambda cell, *a, **k: runs.append(cell.name) or real(cell, *a, **k))
    seen = []
    out = cs.layout_sparams(doc, _FAST, progress=seen.append)
    fdtd = [r for r in out.rows if r.how == cs.FDTD]
    assert len(fdtd) == 1 and fdtd[0].cell_name == "bend_euler"
    assert len(fdtd[0].instances) == 2 and len(runs) == 1      # two bends, one run
    assert set(out.models) == set(fdtd[0].instances)
    assert any(r.how == cs.ANALYTIC and r.cell_name == "straight" for r in out.rows)
    assert seen and "bend_euler" in seen[0]
    # The point of it all: the largest grid is far smaller than the layout's.
    assert 0 < out.peak_cells < out.whole_layout_cells
    assert "largest grid" in out.summary()
    # A second pass costs no simulation.
    again = cs.layout_sparams(doc, _FAST)
    assert len(runs) == 1 and [r.how for r in again.rows if r.sparams] == [cs.CACHED]
    # ... unless asked to measure afresh.
    cs.layout_sparams(doc, cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07,
                                                 use_cache=False))
    assert len(runs) == 2


def test_a_component_over_the_limit_keeps_its_model_or_stops_the_build(qapp):
    doc = _bent_route_document()
    tiny = cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07, max_cells=100)
    out = cs.layout_sparams(doc, tiny)
    assert not out.models and any("over the limit" in n for n in out.notes())
    # A part photonfdtd has no model for cannot fall back.
    doc.add_instance("crossing", {}, x=100.0)
    with pytest.raises(cs.UnsupportedComponent, match="crossing"):
        cs.layout_sparams(doc, tiny)


def test_only_unmodelled_leaves_bends_to_photonfdtd(qapp):
    doc = _bent_route_document()
    cfg = cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07, only_unmodelled=True)
    out = cs.layout_sparams(doc, cfg)
    assert not out.models and out.summary().startswith("No component")


def test_cancel_stops_between_runs(qapp):
    doc = _bent_route_document()
    with pytest.raises(cs.Cancelled):
        cs.layout_sparams(doc, _FAST, cancel=lambda: True)


def test_the_circuit_uses_the_measured_components(qapp):
    doc = _bent_route_document()
    ideal = cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0))
    circ = cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0), fdtd=_FAST)
    assert set(circ.fdtd.models) <= set(circ.chip.models)
    kinds = {circ.model_kind(n) for n in circ.fdtd.models}
    assert kinds == {"S-matrix, FDTD"}
    assert ideal.fdtd is None
    first, last = circ.optical_ports[0], circ.optical_ports[-1]
    lossless = cc.operating_point(ideal, {}, {first: 1.0}, 1.55).power[last][0]
    measured = cc.operating_point(circ, {}, {first: 1.0}, 1.55).power[last][0]
    assert lossless == pytest.approx(1e-3, rel=1e-6)
    assert 0.3e-3 < measured < 1.0e-3 * 1.02     # the bends' loss is now in it


def test_circuit_tab_builds_component_by_component(qapp):
    import time

    from PySide6.QtWidgets import QApplication

    from phidler.panels.circuit_tab import CircuitTab

    tab = CircuitTab(_bent_route_document())
    assert tab.fdtd_config() is None                      # off by default
    tab.fdtd_group.setChecked(True)
    tab.fdtd_cell_spin.setValue(0.07)
    tab.fdtd_points_spin.setValue(1)
    cfg = tab.fdtd_config()
    assert cfg.eim and cfg.cell_size_um == pytest.approx(0.07) and cfg.n_wavelengths == 1
    tab._on_build()
    assert tab.stop_button.isEnabled()
    t0 = time.time()
    while tab.is_busy() and time.time() - t0 < 120:
        QApplication.processEvents()
        time.sleep(0.01)
    assert tab.circuit is not None and tab.circuit.fdtd is not None, tab.status_label.text()
    assert not tab.stop_button.isEnabled()
    models = [tab.components_table.item(i, 1).text() for i in range(tab.components_table.rowCount())]
    assert "S-matrix, FDTD" in models
    assert "largest grid" in tab.status_label.text()
    tab.shutdown()


# -- driven components ------------------------------------------------------

V_PI = 5.0


@pytest.fixture
def fast_lnoi(monkeypatch):
    """Ideal LN models with V_pi = 5 V in place of the cross-section solves."""
    from photonfdtd import lnoi

    def fake_model(cell, **_):
        rec = dict(cell.info["lnoi"])
        lam = 1.55e-6
        seg = rec["segments"][0]
        L = (seg["x1"] - seg["x0"]) * 1e-6
        if rec["kind"] == "phase_shifter":
            return lnoi.PhaseShifterModel(length=L, wavelength=lam,
                                          dneff_dV=lam / (2 * V_PI * L), n_eff=1.9)
        d = lam / (2 * V_PI * L) / 2
        return lnoi.SwitchModel(kind=rec["kind"], wavelength=lam,
                                kappa=np.pi / (4 * rec["coupler"]["length"] * 1e-6),
                                slopes=np.array([[-d, 0, 0], [d, 0, 0]]), rec=rec,
                                electrode_names=["sig", "gnd_top", "gnd_bot"], signals=["sig"],
                                info=dict(n_even=1.9, n_odd=1.9, coupling_length=0.0, vpi=V_PI))
    monkeypatch.setattr(lnoi, "model", fake_model)


def test_an_index_change_shifts_the_phase_by_its_length(qapp):
    doc = LayoutDocument()
    cell = _cell("straight", length=8.0)
    cfg = cs.ComponentSimConfig(n_wavelengths=1)
    off = cs.simulate_component(cell, doc.project_settings, cfg)
    dn = 0.02
    on = cs.simulate_component(cell, doc.project_settings, cfg,
                               index_change=[((0.0, -2.0, 8.0, 2.0), dn)])
    assert on.key != off.key
    shift = -np.angle(on.s[("o1", "o2")][0] / off.s[("o1", "o2")][0])
    assert shift == pytest.approx(2 * np.pi * dn * 8.0 / 1.55, rel=0.04)
    assert abs(on.s[("o1", "o2")][0]) ** 2 == pytest.approx(1.0, abs=0.01)


def test_a_heater_is_planned_from_its_model(qapp):
    import photonfdtd.circuit as pc

    cell = _cell("straight_heater_metal_undercut", length=100.0)
    model = pc.heater(P_pi=0.02, resistance=500.0, length=100.0)
    plan = cs.drive_plan(cell, {}, model, cs.ComponentSimConfig())
    assert plan.kind == "P" and plan.axes == ("P",)
    assert plan.axis_values((0.0, 2.0, -2.0)) == (0.0, pytest.approx(4.0 / 500.0))
    (patch,) = plan.patches
    x0, y0, x1, y1 = patch.rect
    assert (x0, x1) == pytest.approx((cell.ports["o1"].center[0], cell.ports["o2"].center[0]))
    assert y0 < 0.0 < y1
    # P_pi over the heater's length is a phase of pi.
    assert 2 * np.pi * patch.dn({"P": 0.02}) * (x1 - x0) / 1.55 == pytest.approx(np.pi, rel=1e-6)


def test_a_switch_is_driven_under_each_arm(qapp, fast_lnoi):
    cc.register_cells()
    cell = _cell("lnoi_mzi_switch", arm_length=100.0)
    builtin = SimpleNamespace(ports=("o1", "o2", "o3", "o4"))
    plan = cs.drive_plan(cell, dict(cell.info), builtin, cs.ComponentSimConfig())
    assert plan.axes == ("sig",) and plan.reference == ("gnd_top", "gnd_bot")
    lower, upper = plan.patches
    assert lower.rect[3] < 0.0 < upper.rect[1]                 # one arm each, not overlapping
    assert lower.dn({"sig": 1.0}) == pytest.approx(-upper.dn({"sig": 1.0}))   # push-pull
    phase = 2 * np.pi * (upper.dn({"sig": V_PI}) - lower.dn({"sig": V_PI})) * 100.0 / 1.55
    assert phase == pytest.approx(np.pi, rel=1e-6)


def test_drive_states_are_exact_and_interpolated_in_between():
    def state(t):
        return cs.ComponentSParams("k", ("o1", "o2"), np.array([1.55]),
                                   {("o1", "o2"): np.array([t]), ("o2", "o1"): np.array([t])})

    plan = cs.DrivePlan("V", ("sig",), ("gnd",), [])
    d = cs.DrivenSParams(plan, ((0.0, 4.0),), {(0,): state(1.0), (1,): state(0.5 * np.exp(-2.0j))})
    at = lambda v: d.sdict(1.55, {"sig": v})[("o1", "o2")][0]
    assert at(0.0) == pytest.approx(1.0) and at(4.0) == pytest.approx(0.5 * np.exp(-2.0j))
    assert at(2.0) == pytest.approx(0.75 * np.exp(-1.0j))     # magnitude and phase halfway
    assert at(9.0) == pytest.approx(at(4.0))                   # held outside the states
    import photonfdtd.circuit as pc

    builtin = pc.Modulator(lambda wl, **_: {}, ("o1", "o2"),
                           {"sig": ("s",), "gnd": ("g",)}, bandwidth=1e9)
    model = d.model(builtin)
    assert isinstance(model, pc.Modulator) and model.bandwidth == 1e9
    # Only the signal's voltage over the ground's counts.
    lifted = model.sdict(1.55, V={"sig": 7.0, "gnd": 5.0})[("o1", "o2")][0]
    assert lifted == pytest.approx(at(2.0))


def _phase_shifter_document():
    cc.register_cells()
    doc = LayoutDocument()
    doc.add_instance("lnoi_phase_shifter", {"length": 12.0})
    return doc


def test_a_modulator_is_measured_at_each_voltage(qapp, fast_lnoi, monkeypatch, tmp_path):
    import photonfdtd.circuit as pc

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    doc = _phase_shifter_document()
    plain = cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0), fdtd=_FAST)
    assert plain.fdtd.models == {}                             # no voltages: photonfdtd's model
    cfg = cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07, drive_voltages=(0.0, 4.0))
    circ = cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0), fdtd=cfg)
    (name,) = circ.fdtd.models
    model = circ.chip.models[name]
    assert isinstance(model, pc.Modulator) and set(model.conductors) == {"sig", "gnd"}
    assert circ.model_kind(name) == "S-matrix, FDTD"
    assert "0, 4 V on sig" in circ.fdtd.notes()[0]
    ideal = plain.chip.models[name]

    def shift(m, v):
        t = m.sdict(1.55, V={"sig": v})[("o1", "o2")][0] / m.sdict(1.55)[("o1", "o2")][0]
        assert abs(t) == pytest.approx(1.0, abs=0.02)
        return -np.angle(t)

    assert shift(ideal, 4.0) == pytest.approx(np.pi * 4.0 / V_PI)
    assert shift(model, 4.0) == pytest.approx(shift(ideal, 4.0), rel=0.05)
    assert shift(model, 2.0) == pytest.approx(shift(model, 4.0) / 2)

    # Both states are cached; a third voltage costs one more state only.
    runs = []
    real = cs.simulate_component
    monkeypatch.setattr(cs, "simulate_component", lambda *a, **k: runs.append(1) or real(*a, **k))
    again = cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0), fdtd=cfg)
    assert not runs and again.fdtd.rows[0].how == cs.CACHED
    wider = cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07,
                                  drive_voltages=(0.0, 2.0, 4.0))
    cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0), fdtd=wider)
    assert len(runs) == 1


def test_a_driven_part_that_cannot_be_run_keeps_its_model(qapp, fast_lnoi):
    doc = _phase_shifter_document()
    cfg = cs.ComponentSimConfig(n_wavelengths=1, cell_size_um=0.07, max_cells=1000,
                                drive_voltages=(0.0, 4.0))
    circ = cc.build_circuit(doc, cc.CircuitConfig(loss_db_cm=0.0), fdtd=cfg)
    assert circ.fdtd.models == {}
    (note,) = circ.fdtd.notes()
    assert "over the limit" in note and "photonfdtd's model is used" in note


def test_the_tab_reads_the_driven_parts_voltages(qapp):
    from phidler.panels.circuit_tab import CircuitTab

    tab = CircuitTab(_bent_route_document())
    tab.fdtd_group.setChecked(True)
    assert tab.fdtd_config().drive_voltages == ()
    tab.fdtd_drive_edit.setText("0, 5")
    assert tab.fdtd_config().drive_voltages == (0.0, 5.0)
    tab.fdtd_drive_edit.setText("on")
    tab._on_build()
    assert not tab.is_busy() and "not numbers" in tab.status_label.text()
    tab.shutdown()
