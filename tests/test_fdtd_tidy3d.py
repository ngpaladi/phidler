"""The Tidy3D engine: availability gating, building a tidy3d.Simulation from the
layout, converting its result to the display's stub contract, and the run path
(with the cloud call mocked — a real run needs a Flexcompute account + credits).
"""

from __future__ import annotations

import numpy as np
import pytest

from phidler import fdtd_tidy3d as ft
from phidler.fdtd_sim import FdtdParams, SourceSpec
from phidler.model.document import LayoutDocument


# -- availability gating (no tidy3d needed) ------------------------------------


def test_availability_helpers_return_bools():
    assert isinstance(ft.tidy3d_available(), bool)
    assert isinstance(ft.api_key_configured(), bool)
    reason = ft.unavailable_reason()
    assert reason is None or isinstance(reason, str)
    # Ready exactly when the package is importable AND a key is configured.
    assert (reason is None) == (ft.tidy3d_available() and ft.api_key_configured())


td = pytest.importorskip("tidy3d", reason="tidy3d extra not installed")


def _doc():
    from gdsfactory.gpdk import get_generic_pdk

    get_generic_pdk().activate()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 6.0, "width": 0.5})
    return doc


# -- building the simulation ---------------------------------------------------


def test_build_simulation_has_waveguide_source_and_time_monitor():
    doc = _doc()
    sim = ft.build_tidy3d_simulation(doc, FdtdParams(cell_size_um=0.1))
    assert len(sim.structures) >= 1  # the waveguide core
    assert len(sim.sources) == 1  # default left-edge dipole
    assert [m.name for m in sim.monitors] == ["field"]
    assert isinstance(sim.monitors[0], td.FieldTimeMonitor)
    # cladding is the background medium; the waveguide is the higher-index core.
    core_n = doc.project_settings.core_index
    clad_n = doc.project_settings.clad_index
    assert abs(sim.medium.permittivity ** 0.5 - clad_n) < 1e-6
    assert abs(sim.structures[0].medium.permittivity ** 0.5 - core_n) < 1e-6
    # the monitor is a z=0 plane (size_z == 0)
    assert sim.monitors[0].size[2] == 0.0


def test_uniform_grid_uses_the_cell_size():
    doc = _doc()
    fine = ft.build_tidy3d_simulation(doc, FdtdParams(cell_size_um=0.05), cfg=ft.Tidy3dConfig(use_cell_size=True))
    coarse = ft.build_tidy3d_simulation(doc, FdtdParams(cell_size_um=0.2), cfg=ft.Tidy3dConfig(use_cell_size=True))
    assert list(fine.grid.num_cells)[0] > list(coarse.grid.num_cells)[0]


def test_region_clips_the_domain():
    doc = _doc()
    whole = ft.build_tidy3d_simulation(doc, FdtdParams(cell_size_um=0.1))
    region = ft.build_tidy3d_simulation(doc, FdtdParams(cell_size_um=0.1), region_um=(0.0, -1.0, 2.0, 1.0))
    assert region.size[0] < whole.size[0]  # smaller x window


def test_non_dipole_source_raises_a_clear_error():
    doc = _doc()
    params = FdtdParams(sources=(SourceSpec(x_um=1.0, y_um=0.0, kind="single_photon", core_width_um=0.5),))
    with pytest.raises(RuntimeError, match="dipole"):
        ft.build_tidy3d_simulation(doc, params)


# -- result conversion ---------------------------------------------------------


def _fake_sim_data(nx=8, ny=5, nt=7):
    import xarray as xr
    from types import SimpleNamespace

    ez = xr.DataArray(
        np.random.rand(nx, ny, 1, nt).astype("float32"),
        dims=("x", "y", "z", "t"),
        coords={"x": np.linspace(0, 7, nx), "y": np.linspace(-1, 1, ny), "z": [0.0], "t": np.linspace(0, 1e-13, nt)},
    )
    return {"field": SimpleNamespace(Ez=ez)}, (nx, ny, nt)


def test_result_stubs_shape_and_coords():
    from types import SimpleNamespace

    sim_data, (nx, ny, nt) = _fake_sim_data()
    fake_sim = SimpleNamespace(grid=SimpleNamespace(num_cells=[nx, ny, 9]))
    sim_stub, result_stub, elapsed = ft._result_stubs(fake_sim, sim_data, 1.5)

    movie = result_stub.fields["field"]["Ez"]
    assert movie.shape == (nt, ny, nx)  # (frames, ny, nx) — what the display reads
    assert sim_stub.grid.shape == (nx, ny, 9)
    assert len(sim_stub.grid.coords[0]) == nx
    assert sim_stub.engine == "tidy3d"
    assert elapsed == 1.5


# -- the run path (cloud call mocked) ------------------------------------------


def test_run_on_tidy3d_mocked(monkeypatch):
    doc = _doc()
    sim_data, _ = _fake_sim_data()

    captured = {}

    def fake_run(sim, task_name=None, verbose=True, progress_callback_upload=None,
                 progress_callback_download=None, **kw):
        captured["task_name"] = task_name
        if progress_callback_upload:
            progress_callback_upload(1.0)
        if progress_callback_download:
            progress_callback_download(1.0)
        return sim_data

    import tidy3d.web

    monkeypatch.setattr(tidy3d.web, "run", fake_run)

    ticks: list[tuple[int, int]] = []
    sim_stub, result_stub, elapsed = ft.run_on_tidy3d(
        doc, FdtdParams(cell_size_um=0.1), None, ft.Tidy3dConfig(task_name="unit-test"),
        progress_callback=lambda i, n: ticks.append((i, n)),
    )
    assert captured["task_name"] == "unit-test"
    assert result_stub.fields["field"]["Ez"].ndim == 3
    assert sim_stub.engine == "tidy3d"
    assert ticks and ticks[-1] == (100, 100)  # download phase drives it to 100%
