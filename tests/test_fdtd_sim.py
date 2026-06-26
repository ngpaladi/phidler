import math

import pytest

from phidler.fdtd_sim import (
    FdtdParams,
    ModeProfileParams,
    SourceSpec,
    build_mode_solver,
    build_simulation,
    build_source,
    effective_clad_thickness_um,
    estimate_grid_cell_count,
    estimate_run_seconds,
    mode_confinement,
    nearest_z_index,
    photon_energy_ev_from_wavelength_um,
    run_simulation,
    solve_mode_profile,
    wavelength_um_from_photon_energy_ev,
)
from phidler.model.document import LayoutDocument, ProjectSettings

# A tiny, fast domain throughout: short component, coarse cell size, short
# run time. These tests prove the wiring (does it build, does it run, is
# the result shaped as expected) — not any physical correctness of the
# FDTD solve itself, which is photonfdtd's own responsibility to get right.
_FAST_PARAMS = FdtdParams(wavelength_um=1.55, cell_size_um=0.1, run_time_fs=3.0, padding_um=0.3, pulse_fwhm_fs=1.0)


def _tiny_document() -> LayoutDocument:
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 1.5, "width": 0.5})
    doc.project_settings.clad_thickness_um = 1.0
    return doc


def test_build_simulation_on_empty_layout_raises_a_clear_error(qapp):
    doc = LayoutDocument()
    with pytest.raises(ValueError, match="empty layout"):
        build_simulation(doc, _FAST_PARAMS)


def test_estimate_grid_cell_count_on_empty_layout_raises(qapp):
    doc = LayoutDocument()
    with pytest.raises(ValueError, match="empty layout"):
        estimate_grid_cell_count(doc, _FAST_PARAMS)


def test_build_simulation_is_true_3d_not_collapsed(qapp):
    """The original version of this function forced z_size=0 (quasi-2D)
    for speed, which made cladding thickness inert. That was intentionally
    replaced: this is the opposite regression guard, confirming the
    vertical dimension is now genuinely resolved with more than one cell."""
    doc = _tiny_document()
    sim = build_simulation(doc, _FAST_PARAMS)
    assert sim.grid.ndim == 3
    assert sim.grid.shape[2] > 1


def test_build_simulation_clad_thickness_changes_the_z_extent(qapp):
    doc_thin = _tiny_document()
    doc_thin.project_settings.clad_thickness_um = 0.5
    doc_thick = _tiny_document()
    doc_thick.project_settings.clad_thickness_um = 3.0

    sim_thin = build_simulation(doc_thin, _FAST_PARAMS)
    sim_thick = build_simulation(doc_thick, _FAST_PARAMS)
    assert sim_thick.grid.size[2] > sim_thin.grid.size[2]


def test_build_simulation_resolves_lateral_cladding_at_core_height(qapp):
    """Regression guard for a real bug found during development: background
    slabs only covering above/below the core left the core's own z-range
    unstamped outside the waveguide polygon, silently defaulting to vacuum
    (eps_r=1) instead of cladding there. Caught by directly inspecting
    sim.eps_r, not assumed from the adapter's docs."""
    doc = _tiny_document()
    sim = build_simulation(doc, _FAST_PARAMS)
    mid_core_idx = nearest_z_index(sim.grid, 0.0)
    eps_at_mid_core = set(sim.eps_r[:, :, mid_core_idx].flatten().tolist())
    assert 1.0 not in {round(v, 3) for v in eps_at_mid_core}


def test_build_simulation_uses_project_settings_indices(qapp):
    """The material stack comes from the document's own ProjectSettings —
    not a hardcoded default — so switching the platform picker (Silicon,
    SiN, LN, LT) actually changes what gets simulated."""
    doc = _tiny_document()
    doc.project_settings.core_index = 2.211
    doc.project_settings.clad_index = 1.44
    sim = build_simulation(doc, _FAST_PARAMS)
    core_eps_values = {round(s.medium.eps_r, 3) for s in sim.structures if s.medium.eps_r > 2.0}
    assert round(2.211**2, 3) in core_eps_values


def test_build_simulation_attaches_a_default_source_and_monitor_when_none_given(qapp):
    """from_gdsfactory itself returns sources=[]/monitors=[] — confirmed
    by reading its own docstring — so build_simulation must add both, or
    running it would produce an all-zero field with nothing exciting it."""
    doc = _tiny_document()
    sim = build_simulation(doc, _FAST_PARAMS)
    assert len(sim.sources) >= 1
    assert len(sim.monitors) == 1
    assert sim.monitors[0].name == "field"


def test_build_simulation_uses_all_provided_sources(qapp):
    doc = _tiny_document()
    specs = (
        SourceSpec(x_um=0.0, y_um=0.0, wavelength_um=1.55),
        SourceSpec(x_um=0.5, y_um=0.0, wavelength_um=1.31),
    )
    params = FdtdParams(
        wavelength_um=1.55, cell_size_um=0.1, run_time_fs=3.0, padding_um=0.3, pulse_fwhm_fs=1.0, sources=specs
    )
    sim = build_simulation(doc, params)
    assert len(sim.sources) == 2


def test_run_simulation_produces_a_nonempty_field_array_of_the_expected_shape(qapp):
    doc = _tiny_document()
    sim = build_simulation(doc, _FAST_PARAMS)
    result = run_simulation(sim)

    arr = result.fields["field"]["Ez"]
    assert arr.ndim == 4  # (n_frames, nx, ny, nz)
    assert arr.shape[0] > 0  # at least one snapshot
    assert arr.shape[1] == sim.grid.shape[0]
    assert arr.shape[2] == sim.grid.shape[1]
    assert arr.shape[3] == sim.grid.shape[2]
    assert (arr != 0).any()  # the source actually excited something


def test_estimate_grid_cell_count_matches_the_actual_built_grid(qapp):
    doc = _tiny_document()
    estimated = estimate_grid_cell_count(doc, _FAST_PARAMS)
    sim = build_simulation(doc, _FAST_PARAMS)
    actual = int(sim.grid.shape[0]) * int(sim.grid.shape[1]) * int(sim.grid.shape[2])
    assert estimated == actual


def test_estimate_grid_cell_count_is_cheap_and_does_not_build_structures(qapp):
    """The whole point of this function is to be a fast pre-flight check
    callable before committing to an expensive real run — it must not
    itself rasterize geometry or run any timesteps."""
    import time

    doc = _tiny_document()
    t0 = time.time()
    estimate_grid_cell_count(doc, FdtdParams(cell_size_um=0.02))  # finer grid, would be slow to actually build+run
    elapsed = time.time() - t0
    assert elapsed < 1.0


def test_estimate_run_seconds_scales_with_cells_and_steps():
    base = estimate_run_seconds((10, 10, 10), 100)
    assert estimate_run_seconds((20, 10, 10), 100) == pytest.approx(base * 2)
    assert estimate_run_seconds((10, 10, 10), 200) == pytest.approx(base * 2)


def test_fdtd_params_resolves_defaults_relative_to_wavelength_and_pulse_width():
    params = FdtdParams(wavelength_um=1.55, pulse_fwhm_fs=3.0)
    assert params.resolved_cell_size_um() == pytest.approx(1.55 / 20)
    assert params.resolved_run_time_fs() == pytest.approx(3.0 * 8)


def test_fdtd_params_explicit_values_override_defaults():
    params = FdtdParams(cell_size_um=0.123, run_time_fs=99.0)
    assert params.resolved_cell_size_um() == pytest.approx(0.123)
    assert params.resolved_run_time_fs() == pytest.approx(99.0)


def test_wavelength_photon_energy_conversion_round_trips():
    wavelength_um = 1.55
    energy_ev = photon_energy_ev_from_wavelength_um(wavelength_um)
    assert energy_ev == pytest.approx(0.7997, abs=1e-3)  # ~0.8eV at 1550nm, a standard telecom benchmark value
    back = wavelength_um_from_photon_energy_ev(energy_ev)
    assert back == pytest.approx(wavelength_um, rel=1e-9)


# -- mode solver --------------------------------------------------------- #

_SOI_SETTINGS = ProjectSettings(core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=2.0)
_MODE_PARAMS = ModeProfileParams(wavelength_um=1.55, core_width_um=0.5, cell_size_um=0.04)


def test_mode_solver_produces_a_believable_n_eff(qapp):
    solver = build_mode_solver(_SOI_SETTINGS, _MODE_PARAMS)
    result = solve_mode_profile(solver)
    # standard SOI strip waveguide benchmark range; this is a scalar
    # approximation so won't match vectorial literature values exactly
    assert 2.0 < result.n_eff[0] < 3.0


def test_mode_confinement_flags_a_too_thin_cladding(qapp):
    """Regression guard for the empirical finding that motivated the whole
    mode-solver feature: a too-thin cladding visibly truncates the mode
    against the solver's zero-amplitude domain boundary."""
    thin_settings = ProjectSettings(core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=0.05)
    solver = build_mode_solver(thin_settings, _MODE_PARAMS)
    result = solve_mode_profile(solver)
    check = mode_confinement(result)
    assert check.well_confined is False
    assert check.edge_to_peak_ratio > 0.05


def test_mode_confinement_passes_with_adequate_cladding(qapp):
    solver = build_mode_solver(_SOI_SETTINGS, _MODE_PARAMS)
    result = solve_mode_profile(solver)
    check = mode_confinement(result)
    assert check.well_confined is True
    assert check.edge_to_peak_ratio < 0.01


def test_effective_clad_thickness_uses_finite_value_unless_infinite():
    finite = ProjectSettings(clad_thickness_um=2.0, clad_infinite=False)
    assert effective_clad_thickness_um(finite, wavelength_um=1.55) == 2.0

    infinite = ProjectSettings(clad_thickness_um=2.0, clad_infinite=True)
    # Ignores the (small) thickness, scales with wavelength, much larger.
    assert effective_clad_thickness_um(infinite, wavelength_um=1.55) > 2.0
    assert effective_clad_thickness_um(infinite, wavelength_um=1.55) == pytest.approx(
        6.0 * 1.55
    )


def test_infinite_cladding_rescues_a_too_thin_setting_in_the_mode_solver(qapp):
    """A 0.05µm cladding truncates the mode (the too-thin test above), but
    turning on infinite-cladding mode ignores that thickness and solves on a
    domain large enough that the same waveguide is well confined."""
    thin_but_infinite = ProjectSettings(
        core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=0.05, clad_infinite=True
    )
    solver = build_mode_solver(thin_but_infinite, _MODE_PARAMS)
    result = solve_mode_profile(solver)
    check = mode_confinement(result, infinite_clad=True)
    assert check.well_confined is True


def test_infinite_cladding_enlarges_the_fdtd_z_extent(qapp):
    doc_finite = _tiny_document()
    doc_finite.project_settings.clad_thickness_um = 0.5
    doc_infinite = _tiny_document()
    doc_infinite.project_settings.clad_thickness_um = 0.5
    doc_infinite.project_settings.clad_infinite = True

    sim_finite = build_simulation(doc_finite, _FAST_PARAMS)
    sim_infinite = build_simulation(doc_infinite, _FAST_PARAMS)
    assert sim_infinite.grid.size[2] > sim_finite.grid.size[2]


def test_infinite_cladding_grid_estimate_grows_too(qapp):
    doc_finite = _tiny_document()
    doc_finite.project_settings.clad_thickness_um = 0.5
    doc_infinite = _tiny_document()
    doc_infinite.project_settings.clad_thickness_um = 0.5
    doc_infinite.project_settings.clad_infinite = True

    assert estimate_grid_cell_count(doc_infinite, _FAST_PARAMS) > estimate_grid_cell_count(
        doc_finite, _FAST_PARAMS
    )


def test_confinement_message_does_not_suggest_thicker_cladding_in_infinite_mode():
    # A weakly-guided result in infinite mode: not well confined, but the
    # message must not tell the user to raise a thickness that is ignored.
    from phidler.fdtd_sim import ConfinementCheck

    infinite = ConfinementCheck(edge_to_peak_ratio=0.2, well_confined=False, infinite_clad=True)
    assert "increasing cladding thickness" not in infinite.message.lower()
    assert "weakly guided" in infinite.message.lower()

    finite = ConfinementCheck(edge_to_peak_ratio=0.2, well_confined=False, infinite_clad=False)
    assert "increasing cladding thickness" in finite.message.lower()


def test_mode_solver_is_fast_at_default_resolution(qapp):
    import time

    t0 = time.time()
    solver = build_mode_solver(_SOI_SETTINGS, _MODE_PARAMS)
    solve_mode_profile(solver)
    assert time.time() - t0 < 5.0


# -- sources -------------------------------------------------------------- #


def test_build_source_dipole_always_works(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="dipole", wavelength_um=1.55)
    source = build_source(_SOI_SETTINGS, spec)
    assert source.component == "Ez"


def test_build_source_single_photon_requires_core_width(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="single_photon", wavelength_um=1.55, core_width_um=None)
    with pytest.raises(ValueError, match="core_width_um"):
        build_source(_SOI_SETTINGS, spec)


def test_build_source_single_photon_builds_a_mode_injected_source(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="single_photon", wavelength_um=1.55, core_width_um=0.5)
    source = build_source(_SOI_SETTINGS, spec)
    assert source.component == "Ey"
    assert source.n_eff > 1.0


def test_build_source_unknown_kind_raises(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="bogus", wavelength_um=1.55)
    with pytest.raises(ValueError, match="unknown source kind"):
        build_source(_SOI_SETTINGS, spec)


def test_build_source_scripted_requires_a_script(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="scripted", script=None)
    with pytest.raises(ValueError, match="scripted sources need a script"):
        build_source(_SOI_SETTINGS, spec)


def test_build_source_scripted_evaluates_the_expression(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="scripted", script="np.sin(2*np.pi*1.93e14*t)")
    source = build_source(_SOI_SETTINGS, spec)
    assert source.component == "Ez"
    import numpy as np

    values = source.waveform(np.array([0.0, 1e-15, 2e-15]))
    assert np.isfinite(values).all()


def test_run_simulation_with_a_scripted_source_produces_a_finite_field(qapp):
    doc = _tiny_document()
    spec = SourceSpec(
        x_um=0.0, y_um=0.0, kind="scripted", script="np.sin(2*np.pi*1.93e14*t) * np.exp(-((t-2e-15)/1e-15)**2)"
    )
    params = FdtdParams(
        wavelength_um=1.55, cell_size_um=0.1, run_time_fs=3.0, padding_um=0.3, pulse_fwhm_fs=1.0, sources=(spec,)
    )
    sim = build_simulation(doc, params)
    result = run_simulation(sim)

    import numpy as np

    arr = result.fields["field"]["Ez"]
    assert np.isfinite(arr).all()
    assert (arr != 0).any()


def test_build_source_photon_count_scales_amplitude_by_sqrt_n(qapp):
    """The N-fold *energy* scaling (not N^2-fold, which coherently stacking
    N copies at the same place/phase would give) was verified directly via
    a FluxMonitor during development — see fdtd_sim.build_source's
    docstring. This test pins the amplitude-level mechanism: a ModeSource
    at photon_count=N has sqrt(N) times the single-photon amplitude."""
    spec1 = SourceSpec(x_um=0.0, y_um=0.0, kind="single_photon", wavelength_um=1.55, core_width_um=0.5, photon_count=1)
    spec4 = SourceSpec(x_um=0.0, y_um=0.0, kind="single_photon", wavelength_um=1.55, core_width_um=0.5, photon_count=4)

    source1 = build_source(_SOI_SETTINGS, spec1)
    source4 = build_source(_SOI_SETTINGS, spec4)

    amp1 = source1.peak_field * (2.0 if source1.bidirectional else 1.0)
    amp4 = source4.amplitude
    assert amp4 == pytest.approx(amp1 * math.sqrt(4), rel=1e-9)


def test_nearest_z_index_finds_mid_core_height(qapp):
    doc = _tiny_document()
    sim = build_simulation(doc, _FAST_PARAMS)
    idx = nearest_z_index(sim.grid, 0.0)
    assert 0 <= idx < sim.grid.shape[2]
    # at mid-core height, the z coordinate found should be close to 0
    assert abs(sim.grid.coords[2][idx]) < sim.grid.cell_size[2]
