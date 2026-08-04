import dataclasses
import math

import pytest

from phidler.fdtd_sim import (
    FdtdParams,
    ModeProfileParams,
    SourceSpec,
    build_eim_reduction,
    build_mode_solver,
    build_simulation,
    build_source,
    effective_clad_thickness_um,
    estimate_grid_cell_count,
    estimate_run_seconds,
    limit_solver_threads,
    mode_confinement,
    nearest_z_index,
    photon_energy_ev_from_wavelength_um,
    run_simulation,
    rust_available,
    solve_mode_profile,
    wavelength_um_from_photon_energy_ev,
)
from phidler.model.document import EtchLayer, LayoutDocument, ProjectSettings

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


def test_fdtd_propagation_z_is_just_enough_not_the_full_cladding_setting(qapp):
    # The FDTD propagation domain keeps only a few evanescent decay lengths of
    # cladding (for speed), so a much thicker cladding *setting* — which the mode
    # solver still honors for confinement — does not bloat the FDTD z-grid.
    doc_thin = _tiny_document()
    doc_thin.project_settings.clad_thickness_um = 0.5
    doc_thick = _tiny_document()
    doc_thick.project_settings.clad_thickness_um = 3.0

    sim_thin = build_simulation(doc_thin, _FAST_PARAMS)
    sim_thick = build_simulation(doc_thick, _FAST_PARAMS)
    assert sim_thick.grid.size[2] == pytest.approx(sim_thin.grid.size[2])


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


def _rib_document(slab_thickness_um: float | None) -> LayoutDocument:
    """A WG ridge on (1, 0) plus a wider slab on (2, 0). With slab_thickness_um
    set, (2, 0) is configured as an etch layer; otherwise it's left unmapped
    (plain strip), so the two cases can be compared cell-for-cell."""
    doc = LayoutDocument()
    doc.top.add_polygon([(0, -0.25), (4, -0.25), (4, 0.25), (0, 0.25)], layer=(1, 0))  # ridge, 0.5 µm wide
    doc.top.add_polygon([(0, -1.0), (4, -1.0), (4, 1.0), (0, 1.0)], layer=(2, 0))      # slab, 2.0 µm wide
    doc.project_settings = ProjectSettings(
        core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=1.0,
        etch_layers=((EtchLayer(2, 0, slab_thickness_um),) if slab_thickness_um else ()),
    )
    return doc


def test_etch_slab_lands_in_the_rasterized_permittivity(qapp):
    """The whole point of etch layers: a partial-etch slab on (2, 0) must show up
    in sim.eps_r as core material *below the slab height and cladding above it*,
    in the slab-only region. A silent failure (wrong layer key, ignored slab)
    would leave that region identical to the strip case — so this compares the
    two cases cell-for-cell, the way the lateral-cladding bug was caught."""
    import numpy as np

    thickness_um, slab_um = 0.22, 0.08
    params = FdtdParams(wavelength_um=1.55, cell_size_um=0.04, run_time_fs=3.0, padding_um=0.3)
    core_eps, clad_eps = round(3.45**2, 2), round(1.44**2, 2)

    # z probes in the mid-core-centred frame: one inside the bottom slab, one
    # above the slab but still within the full core height.
    z_in_slab = -thickness_um / 2 + slab_um / 2
    z_above_slab = -thickness_um / 2 + slab_um + (thickness_um - slab_um) / 2

    def eps_at(sim, x_um, y_um, z_um):
        xs, ys, _ = sim.grid.coords
        ix = int(np.argmin(np.abs(np.asarray(xs) - (x_um - 2.0) * 1e-6)))  # bbox centre x = 2.0
        iy = int(np.argmin(np.abs(np.asarray(ys) - y_um * 1e-6)))          # bbox centre y = 0
        iz = nearest_z_index(sim.grid, z_um)
        return round(float(np.asarray(sim.eps_r)[ix, iy, iz]), 2)

    sim_rib = build_simulation(_rib_document(slab_um), params)
    sim_strip = build_simulation(_rib_document(None), params)

    X, RIDGE_Y, SLAB_Y = 2.0, 0.0, 0.7  # slab-only probe at y=0.7 (inside slab, outside ridge)

    # (a) The ridge reads core full-height in BOTH cases — pins the WG (1,0)->index
    #     lookup (a regression there would change both, so this is the guard).
    assert eps_at(sim_rib, X, RIDGE_Y, z_in_slab) == core_eps
    assert eps_at(sim_strip, X, RIDGE_Y, z_in_slab) == core_eps
    assert eps_at(sim_rib, X, RIDGE_Y, z_above_slab) == core_eps

    # (b) The slab-only region differs: core below the slab height WITH the etch,
    #     cladding WITHOUT it. Unequal eps here == the slab genuinely landed.
    assert eps_at(sim_rib, X, SLAB_Y, z_in_slab) == core_eps      # the rib slab
    assert eps_at(sim_strip, X, SLAB_Y, z_in_slab) == clad_eps    # strip: nothing there
    assert eps_at(sim_rib, X, SLAB_Y, z_above_slab) == clad_eps   # partial etch: clad above the slab


def test_build_layer_media_map_maps_slab_and_skips_absent_layers(qapp):
    """The layer-media map (no grid build) has the full-height WG plus a
    shorter-z slab for a real etch layer, and silently drops a configured etch
    layer that isn't actually drawn (wrong layer number) rather than mapping a
    wrong kdb index."""
    import photonfdtd as pf

    from phidler.fdtd_sim import build_layer_media_map

    doc = _rib_document(0.08)
    # (2,0) is drawn; (7,0) is not — it must be skipped.
    doc.project_settings.etch_layers = (EtchLayer(2, 0, 0.08), EtchLayer(7, 0, 0.05))

    media = build_layer_media_map(doc, pf.Medium.from_index(3.45), 0.22e-6)
    assert len(media) == 2  # WG + the drawn slab; the absent (7,0) dropped
    spans = sorted(z1 - z0 for _m, (z0, z1) in media.values())
    assert spans[0] < spans[1]  # the slab is shorter than the full-height core


def test_mode_solver_rib_slab_raises_n_eff(qapp):
    """A configured slab makes the mode solver build a rib (ridge + slab) rather
    than a bare strip; the extra lateral high-index material must raise the
    effective index. (Guards against the slab being a silent no-op here too.)"""
    common = dict(core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=1.0)
    strip = ProjectSettings(**common)
    rib = ProjectSettings(**common, etch_layers=(EtchLayer(2, 0, 0.09),))
    params = ModeProfileParams(wavelength_um=1.55, core_width_um=0.5, cell_size_um=0.02)

    n_strip = solve_mode_profile(build_mode_solver(strip, params)).n_eff[0]
    n_rib = solve_mode_profile(build_mode_solver(rib, params)).n_eff[0]
    assert n_rib > n_strip


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
    # The monitor records only the single mid-core (z=0) plane — the only plane
    # the movie shows — so the z axis is size-1, not the full grid depth.
    assert arr.shape[3] == 1
    assert (arr != 0).any()  # the source actually excited something


def test_field_movie_records_only_the_mid_core_plane_and_only_Ez(qapp):
    # The displayed movie is Ez at z=0 (mid-core); recording the whole volume or
    # Ey is wasted memory, so the monitor keeps just that one plane / component.
    import numpy as np

    import photonfdtd as pf

    doc = _tiny_document()
    sim = build_simulation(doc, _FAST_PARAMS)
    result = run_simulation(sim)
    assert set(result.fields["field"]) == {"Ez"}  # Ey not recorded
    assert result.fields["field"]["Ez"].shape[3] == 1  # single z-plane

    # That one plane is the same data a full-volume recording would show at z=0.
    sim_full = build_simulation(doc, _FAST_PARAMS)
    sim_full.monitors.clear()
    sim_full.add_monitor(pf.FieldMonitor(name="field", components=("Ez",), interval=_FAST_PARAMS.monitor_interval))
    full = run_simulation(sim_full).fields["field"]["Ez"]
    zc = nearest_z_index(sim_full.grid, 0.0)
    assert np.allclose(result.fields["field"]["Ez"][:, :, :, 0], full[:, :, :, zc])


def test_region_shrinks_the_grid_and_still_runs(qapp):
    # A sprawling layout: simulating only a sub-region grids far fewer cells.
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 20.0, "width": 0.5}, x=0.0, y=0.0)
    doc.add_instance("mmi1x2", {}, x=200.0, y=150.0)  # far away -> huge full bbox
    params = FdtdParams(cell_size_um=0.08, sources=(SourceSpec(x_um=-8.0, y_um=0.0),))

    region = (-15.0, -6.0, 15.0, 6.0)  # just around the straight
    full = estimate_grid_cell_count(doc, params)
    roi = estimate_grid_cell_count(doc, params, region_um=region)
    assert roi < full / 10  # dramatically fewer cells

    sim = build_simulation(doc, params, region_um=region)
    assert estimate_grid_cell_count(doc, params, region_um=region) == (
        int(sim.grid.shape[0]) * int(sim.grid.shape[1]) * int(sim.grid.shape[2])
    )
    import numpy as np

    assert np.asarray(sim.eps_r).max() > 3.0  # the in-region waveguide core was gridded
    assert (run_simulation(sim).fields["field"]["Ez"] != 0).any()


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
    assert params.resolved_cell_size_um() == pytest.approx(1.55 / 15)
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


def test_infinite_cladding_does_not_enlarge_the_fdtd_propagation_z(qapp):
    # Infinite-cladding is a mode-solver concept (solve on a big domain so the
    # mode doesn't truncate — see the mode-solver test above). The FDTD
    # propagation domain stays thin regardless, so turning it on doesn't blow up
    # the run.
    doc_finite = _tiny_document()
    doc_finite.project_settings.clad_thickness_um = 0.5
    doc_infinite = _tiny_document()
    doc_infinite.project_settings.clad_thickness_um = 0.5
    doc_infinite.project_settings.clad_infinite = True

    sim_finite = build_simulation(doc_finite, _FAST_PARAMS)
    sim_infinite = build_simulation(doc_infinite, _FAST_PARAMS)
    assert sim_infinite.grid.size[2] == pytest.approx(sim_finite.grid.size[2])


def test_infinite_cladding_does_not_grow_the_fdtd_grid_estimate(qapp):
    doc_finite = _tiny_document()
    doc_finite.project_settings.clad_thickness_um = 0.5
    doc_infinite = _tiny_document()
    doc_infinite.project_settings.clad_thickness_um = 0.5
    doc_infinite.project_settings.clad_infinite = True

    assert estimate_grid_cell_count(doc_infinite, _FAST_PARAMS) == estimate_grid_cell_count(
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


def test_build_source_single_photon_uses_rib_profile_when_etched(qapp):
    """A single_photon source solves its launch mode via build_mode_solver,
    which now reads settings.etch_layers — so on a rib platform it launches the
    rib mode (higher n_eff) rather than the strip mode. Pins that coupling."""
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="single_photon", wavelength_um=1.55, core_width_um=0.5)
    rib_settings = ProjectSettings(
        core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=2.0,
        etch_layers=(EtchLayer(2, 0, 0.09),),
    )
    n_strip = build_source(_SOI_SETTINGS, spec).n_eff
    n_rib = build_source(rib_settings, spec).n_eff
    assert n_rib > n_strip


def test_build_source_cherenkov_travels_in_z_out_of_plane(qapp):
    spec = SourceSpec(
        x_um=3.0,
        y_um=1.0,
        kind="cherenkov",
        velocity_beta=0.8,
        direction_deg=0.0,  # straight up, perpendicular to the chip plane
        cherenkov_segments=6,
    )
    dipoles = build_source(_SOI_SETTINGS, spec)
    assert isinstance(dipoles, list) and len(dipoles) == 6

    # The particle punches through in z: x/y stay at the impact point, z climbs
    # monotonically through the dielectric stack, fire delays increase (transit).
    xs = [d.position[0] * 1e6 for d in dipoles]
    ys = [d.position[1] * 1e6 for d in dipoles]
    zs = [d.position[2] * 1e6 for d in dipoles]
    delays = [d.waveform.delay for d in dipoles]
    assert all(math.isclose(x, 3.0, abs_tol=1e-9) for x in xs)
    assert all(math.isclose(y, 1.0, abs_tol=1e-9) for y in ys)
    assert zs == sorted(zs) and zs[0] < 0 < zs[-1]  # crosses the z=0 monitor plane
    assert all(delays[i] < delays[i + 1] for i in range(len(delays) - 1))
    # The track stays within the dielectric stack (core + 2·cladding).
    diel_half = _SOI_SETTINGS.thickness_um / 2 + _SOI_SETTINGS.clad_thickness_um + 1e-6
    assert max(abs(z) for z in zs) <= diel_half + _SOI_SETTINGS.thickness_um


def test_build_source_cherenkov_requires_positive_beta(qapp):
    spec = SourceSpec(x_um=0.0, y_um=0.0, kind="cherenkov", velocity_beta=0.0)
    with pytest.raises(ValueError, match="velocity_beta"):
        build_source(_SOI_SETTINGS, spec)


def test_build_simulation_expands_a_cherenkov_track_into_many_sources(qapp):
    import dataclasses

    doc = _tiny_document()
    spec = SourceSpec(x_um=-0.5, y_um=0.0, kind="cherenkov", cherenkov_segments=8, cherenkov_length_um=1.0)
    sim = build_simulation(doc, dataclasses.replace(_FAST_PARAMS, sources=(spec,)))
    assert len(sim.sources) >= 8  # the track's dipoles were each added


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


def test_limit_solver_threads_leaves_cores_free():
    # The solver thread cap is what keeps a CPU-bound numba run from pinning
    # every core and freezing the desktop. It must always leave headroom (never
    # request more than cpu_count) and never return a nonsense count.
    import os

    n = limit_solver_threads(renice=False)
    cpu = os.cpu_count() or 2
    assert n >= 1
    assert n <= cpu
    # On a machine with cores to spare it should actually reserve some.
    if cpu > 2:
        assert n < cpu


def test_limit_solver_threads_applies_to_numba_when_present():
    numba = pytest.importorskip("numba")
    applied = limit_solver_threads(renice=False)
    assert numba.get_num_threads() == applied


def test_simulation_wavelength_default_matches_the_project_default():
    """The FDTD/mode-solver wavelength defaults must equal the project's
    wavelength default — a project designed at wavelength X should simulate at X
    by default, not at a separately-hardcoded value that could drift."""
    from phidler.fdtd_sim import SimulationConfig
    from phidler.model.document import DEFAULT_WAVELENGTH_UM, ProjectSettings

    project_default = ProjectSettings().wavelength_um
    assert project_default == DEFAULT_WAVELENGTH_UM
    assert FdtdParams().wavelength_um == project_default
    assert ModeProfileParams().wavelength_um == project_default
    assert SimulationConfig().wavelength_um == project_default
    assert SimulationConfig().mode_wavelength_um == project_default


# --- photonfdtd 0.12: compiled Rust cores and the 2.5-D reduction -------------


def test_rust_backend_reaches_the_simulation(qapp):
    """Ticking Rust has to actually select photonfdtd's compiled core — the flag
    is applied by a *rebuild* of the Simulation (from_gdsfactory has no backend
    parameter), which is easy to drop silently."""
    pytest.importorskip("photonfdtd")
    if not rust_available():
        pytest.skip("photonfdtd's Rust extension isn't built in this environment")
    doc = _tiny_document()
    sim = build_simulation(doc, dataclasses.replace(_FAST_PARAMS, use_rust=True))
    assert sim.backend == "rust"
    # Default stays on photonfdtd's own dispatch, unchanged from before.
    assert build_simulation(doc, _FAST_PARAMS).backend == "auto"


def test_rust_backend_agrees_with_numpy(qapp):
    """The whole point of a second stepping core is that it computes the same
    thing. float32 round-off differs (the loop order does), so this is a
    tolerance check, not equality — photonfdtd's own suite puts f32 agreement at
    ~5e-6 and this grid lands well inside that."""
    pytest.importorskip("photonfdtd")
    if not rust_available():
        pytest.skip("photonfdtd's Rust extension isn't built in this environment")
    import numpy as np

    doc = _tiny_document()
    ref = run_simulation(build_simulation(doc, _FAST_PARAMS))
    rust = run_simulation(build_simulation(doc, dataclasses.replace(_FAST_PARAMS, use_rust=True)))
    a = np.asarray(ref.fields["field"]["Ez"])
    b = np.asarray(rust.fields["field"]["Ez"])
    assert a.shape == b.shape
    peak = np.abs(a).max()
    assert peak > 0
    assert np.abs(a - b).max() / peak < 1e-5


def test_subpixel_outranks_rust_so_photonfdtd_never_raises(qapp):
    """photonfdtd's Rust cores raise on subpixel smoothing. The resolver is
    supposed to keep that combination from ever being built, so asking for both
    must produce a working non-Rust Simulation rather than an exception."""
    pytest.importorskip("photonfdtd")
    if not rust_available():
        pytest.skip("photonfdtd's Rust extension isn't built in this environment")
    sim = build_simulation(
        _tiny_document(), dataclasses.replace(_FAST_PARAMS, use_rust=True, subpixel=True),
    )
    assert sim.backend == "auto"
    assert sim.subpixel is True


def test_eim_reduction_orders_the_indices_it_produces(qapp):
    """The reduction only makes sense if the guided core index sits between the
    cladding and the bulk core index — that gap is the in-plane contrast the 2-D
    problem guides on."""
    pytest.importorskip("photonfdtd")
    settings = ProjectSettings()
    red = build_eim_reduction(settings, 1.55)
    assert settings.clad_index < red.n_core < settings.core_index
    assert red.index_contrast == pytest.approx(red.n_core - red.n_background)


def test_eim_background_is_exactly_the_cladding_index(qapp):
    """Where the core is etched clean away the stack is homogeneous cladding, so
    its in-plane index is the cladding index exactly. photonfdtd's own
    EffectiveIndex2D.from_stacks runs its slab solver on that stack too and
    returns a slightly-below-uniform index (~1.4115 against a 1.44 cladding) —
    a discretisation artifact of solving a stack with no mode to find. This is
    the regression guard for not reintroducing that."""
    pytest.importorskip("photonfdtd")
    settings = ProjectSettings()
    red = build_eim_reduction(settings, 1.55)
    assert red.n_background == settings.clad_index


def test_eim_reduction_gives_a_rib_slab_its_own_intermediate_index(qapp):
    """A partial etch leaves part of the core behind, so that region guides
    better than bare cladding and worse than the full core."""
    pytest.importorskip("photonfdtd")
    settings = ProjectSettings(etch_layers=(EtchLayer(layer=2, datatype=0, slab_thickness_um=0.09),))
    red = build_eim_reduction(settings, 1.55)
    assert len(red.n_slab) == 1
    (layer, datatype), n_slab = red.n_slab[0]
    assert (layer, datatype) == (2, 0)
    assert red.n_background < n_slab < red.n_core


def test_eim_build_collapses_the_domain_to_the_chip_plane(qapp):
    """2.5-D's whole value is the cell count, so the built grid must genuinely be
    2-D — and the pre-flight estimate must agree with it, or the feasibility
    guard would refuse whole-circuit runs that fit fine."""
    pytest.importorskip("photonfdtd")
    doc = _tiny_document()
    params = dataclasses.replace(_FAST_PARAMS, eim=True)
    sim = build_simulation(doc, params)
    assert sim.grid.ndim == 2
    assert sim.grid.shape[2] == 1

    cells_2p5d = estimate_grid_cell_count(doc, params)
    cells_3d = estimate_grid_cell_count(doc, _FAST_PARAMS)
    assert cells_2p5d == sim.grid.shape[0] * sim.grid.shape[1]
    assert cells_2p5d < cells_3d
    # The saving is exactly the vertical cell count it dropped.
    assert cells_3d == cells_2p5d * build_simulation(doc, _FAST_PARAMS).grid.shape[2]


def test_eim_simulation_runs_and_records_a_field_movie(qapp):
    pytest.importorskip("photonfdtd")
    import numpy as np

    result = run_simulation(build_simulation(_tiny_document(), dataclasses.replace(_FAST_PARAMS, eim=True)))
    ez = np.asarray(result.fields["field"]["Ez"])
    assert ez.ndim == 4 and ez.shape[0] > 1  # (frames, nx, ny, nz=1)
    assert np.abs(ez).max() > 0


def test_eim_single_photon_source_matches_the_lateral_mode(qapp):
    """In 2.5-D there is no vertical cross-section left to mode-match, so a
    single-photon source has to match the lateral mode of the reduced index map.
    It expands into a line of dipoles across the guide, not a single point."""
    pytest.importorskip("photonfdtd")
    doc = _tiny_document()
    spec = SourceSpec(x_um=0.2, y_um=0.0, kind="single_photon", core_width_um=0.5, wavelength_um=1.55)
    params = dataclasses.replace(_FAST_PARAMS, eim=True, sources=(spec,))
    sim = build_simulation(doc, params)
    assert len(sim.sources) > 1
    # Ez is the TE-like component of a 2-D xy domain — and what the movie records.
    assert {s.component for s in sim.sources} == {"Ez"}


def test_eim_refuses_a_cherenkov_source_instead_of_degrading_it(qapp):
    """A Cherenkov source *is* a track through the vertical stack. 2.5-D has
    collapsed that stack, so there's nothing to sample — better to say so than
    to quietly emit something that looks like a result."""
    pytest.importorskip("photonfdtd")
    spec = SourceSpec(x_um=0.2, y_um=0.0, kind="cherenkov", wavelength_um=1.55)
    params = dataclasses.replace(_FAST_PARAMS, eim=True, sources=(spec,))
    with pytest.raises(ValueError, match="2.5-D"):
        build_simulation(_tiny_document(), params)
    # Still fine in full 3-D.
    sim = build_simulation(_tiny_document(), dataclasses.replace(_FAST_PARAMS, sources=(spec,)))
    assert len(sim.sources) > 1


def test_eim_and_rust_compose(qapp):
    """2.5-D is a method and Rust is a backend — they're independent choices and
    have to work together, which is also the fastest configuration available."""
    pytest.importorskip("photonfdtd")
    if not rust_available():
        pytest.skip("photonfdtd's Rust extension isn't built in this environment")
    import numpy as np

    params = dataclasses.replace(_FAST_PARAMS, eim=True, use_rust=True)
    sim = build_simulation(_tiny_document(), params)
    assert sim.backend == "rust" and sim.grid.ndim == 2
    assert np.abs(np.asarray(run_simulation(sim).fields["field"]["Ez"])).max() > 0
