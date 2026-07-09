"""Memory-safety features of the FDTD builder: the pre-flight feasibility
guard (so an over-large layout fails fast instead of OOM-ing / freezing the
machine), out-of-core stepping wiring, and the monitor spatial-downsample knob."""

import pytest

from phidler.fdtd_sim import (
    FdtdParams,
    SourceSpec,
    build_simulation,
    check_run_feasible,
    estimate_grid_cell_count,
    estimate_out_of_core_disk_gb,
    feasible_cell_budget,
    run_simulation,
    suggest_region_um,
)
from phidler.model.document import LayoutDocument

_FAST = FdtdParams(wavelength_um=1.55, cell_size_um=0.1, run_time_fs=2.0, padding_um=0.3)


def _tiny_doc():
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 2.0, "width": 0.5})
    doc.project_settings.clad_thickness_um = 0.5
    return doc


# -- feasibility guard ------------------------------------------------------


def test_estimate_out_of_core_disk_scales_with_cells():
    assert estimate_out_of_core_disk_gb(0) == 0
    assert estimate_out_of_core_disk_gb(2_000_000_000) > estimate_out_of_core_disk_gb(1_000_000_000) > 0


def test_check_run_feasible_allows_a_small_grid():
    check_run_feasible(1_000_000, _FAST)  # ~0.1 GB, well within any machine — must not raise


def test_check_run_feasible_rejects_a_grid_too_big_for_ram():
    # 10^12 cells is ~100 TB in-core — larger than any workstation.
    with pytest.raises(RuntimeError, match="GB of memory"):
        check_run_feasible(1_000_000_000_000, _FAST)


def test_check_run_feasible_out_of_core_checks_disk_not_ram():
    huge = 1_000_000_000_000  # ~56 TB of scratch out-of-core — exceeds any local disk
    with pytest.raises(RuntimeError, match="scratch disk"):
        check_run_feasible(huge, FdtdParams(out_of_core=True))
    # Out-of-core is bounded by disk, not RAM, so a modest grid passes fine.
    check_run_feasible(1_000_000, FdtdParams(out_of_core=True))


def test_build_simulation_refuses_an_impossible_region(qapp):
    """A region far larger than the machine's RAM must be refused up front with a
    clear message — before from_gdsfactory tries to allocate the full grid."""
    doc = _tiny_doc()
    with pytest.raises(RuntimeError, match="memory|smaller"):
        build_simulation(doc, _FAST, region_um=(0.0, 0.0, 40_000.0, 40_000.0))


# -- out-of-core + monitor wiring ------------------------------------------


def test_build_simulation_out_of_core_forces_numpy_backend(qapp):
    """Out-of-core stepping is NumPy-only, so build must drop GPU/Numba even
    when they're requested (photonfdtd's run_out_of_core rejects them)."""
    params = FdtdParams(
        wavelength_um=1.55, cell_size_um=0.2, run_time_fs=2.0, padding_um=0.3,
        use_numba=True, out_of_core=True,
    )
    sim = build_simulation(_tiny_doc(), params)
    assert getattr(sim, "use_numba", False) is False
    assert getattr(sim, "use_gpu", False) is False


def test_build_simulation_applies_monitor_downsample(qapp):
    sim = build_simulation(_tiny_doc(), FdtdParams(
        wavelength_um=1.55, cell_size_um=0.2, run_time_fs=2.0, padding_um=0.3, monitor_downsample=3,
    ))
    field_monitors = [m for m in sim.monitors if getattr(m, "name", None) == "field"]
    assert field_monitors and field_monitors[0].downsample == 3


def test_run_simulation_passes_out_of_core_through_to_the_solver():
    class FakeSim:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return "result"

    sim = FakeSim()
    assert run_simulation(sim) == "result"
    assert sim.calls[-1] == {}  # in-core: plain run(), no kwargs

    run_simulation(sim, out_of_core=True, tile_cells=4)
    assert sim.calls[-1] == {"out_of_core": True, "tile_cells": 4}


# -- region suggestion (turn "too big" into a runnable region) --------------


def test_feasible_cell_budget_is_positive_and_below_the_refusal_ceiling():
    budget = feasible_cell_budget(_FAST)  # in-core: RAM-based
    assert budget > 0
    # A grid at the suggested budget must pass the run guard (0.6 < 0.8 ceiling).
    check_run_feasible(budget, _FAST)


def test_suggest_region_fits_the_cell_budget(qapp):
    doc = _tiny_doc()
    max_cells = 5_000_000
    region = suggest_region_um(doc, _FAST, max_cells, center_um=(0.0, 0.0))
    left, bottom, right, top = region
    assert right > left and top > bottom
    assert estimate_grid_cell_count(doc, _FAST, region_um=region) <= max_cells


def test_suggest_region_centers_on_the_sources(qapp):
    doc = _tiny_doc()
    params = FdtdParams(
        wavelength_um=1.55, cell_size_um=0.1, run_time_fs=2.0, padding_um=0.3,
        sources=(SourceSpec(x_um=100.0, y_um=50.0),),
    )
    left, bottom, right, top = suggest_region_um(doc, params, 3_000_000)
    assert abs((left + right) / 2 - 100.0) < 1e-6
    assert abs((bottom + top) / 2 - 50.0) < 1e-6
