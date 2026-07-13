"""Run an FDTD simulation on Tidy3D (Flexcompute's cloud solver) instead of
photonfdtd.

This is a second *engine* (not just a transport): where photonfdtd builds a
local Yee-grid ``Simulation`` (see fdtd_sim.build_simulation), this builds a
``tidy3d.Simulation`` from the *same* placed layout — the same core/cladding from
ProjectSettings, the same waveguide polygons, the same source positions — and
submits it to Tidy3D's cloud, then converts the returned time-domain field data
into the exact ``(sim_stub, result_stub, elapsed)`` shape the FDTD window's
display already reads (``sim_stub.grid.coords`` / ``.shape`` and
``result_stub.fields['field']['Ez']``). So the movie playback is unchanged; only
the solver differs.

Tidy3D is a commercial cloud service: it needs the ``tidy3d`` package (the
``tidy3d`` extra) *and* an API key configured for the account, and a run consumes
FlexCredits. Everything here is imported lazily and gated, so plain phidler — and
the photonfdtd engine — work with none of it installed.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from .fdtd_sim import (
    _CORE_LAYER,
    FdtdParams,
    SourceSpec,
    _layer_index_map,
    _layout_bbox_um_tuple,
    fdtd_clad_thickness_um,
)


def tidy3d_available() -> bool:
    """Whether the ``tidy3d`` package can be imported here (the ``tidy3d`` extra).
    Availability of the package is separate from having an API key configured —
    see api_key_configured()."""
    try:
        import tidy3d  # noqa: F401

        return True
    except Exception:
        return False


def api_key_configured() -> bool:
    """Whether a Tidy3D API key is available, so a cloud run can authenticate.
    Tidy3D reads it from the ``SIMCLOUD_APIKEY`` / ``TIDY3D_API_KEY`` environment
    variables or the ``~/.tidy3d/config`` file that ``tidy3d configure`` writes;
    we accept any of those. Never raises."""
    if os.environ.get("SIMCLOUD_APIKEY") or os.environ.get("TIDY3D_API_KEY"):
        return True
    try:
        from tidy3d.web.core.http_util import api_key

        return bool(api_key())
    except Exception:
        pass
    # Fall back to the config file tidy3d writes, so we don't hard-depend on the
    # exact internal helper above across tidy3d versions.
    try:
        cfg = os.path.expanduser("~/.tidy3d/config")
        if os.path.isfile(cfg):
            with open(cfg) as f:
                return "apikey" in f.read().lower()
    except Exception:
        pass
    return False


def unavailable_reason() -> str | None:
    """A one-line, user-facing explanation of why the Tidy3D engine can't run, or
    None when it's ready. Drives the disabled-control tooltip in the FDTD window."""
    if not tidy3d_available():
        return (
            "Tidy3D isn't installed. Add it with `pip install \"phidler[tidy3d]\"` "
            "(or `pip install tidy3d`)."
        )
    if not api_key_configured():
        return (
            "Tidy3D is installed but no API key is configured. Run `tidy3d configure` "
            "(or set TIDY3D_API_KEY) with your Flexcompute account key."
        )
    return None


@dataclass
class Tidy3dConfig:
    """Tidy3D run options set in the FDTD window, kept out of the .phidler project
    (it's an environment/account choice, like the SSH/nereid configs).

    ``task_name`` labels the job in the Tidy3D web UI. ``min_steps_per_wvl`` feeds
    Tidy3D's automatic nonuniform mesher (its native way to size the grid) when
    ``use_cell_size`` is False; otherwise the FDTD window's Cell size drives a
    uniform grid, matching the photonfdtd engine's control."""

    task_name: str = "phidler-fdtd"
    use_cell_size: bool = False
    min_steps_per_wvl: int = 18
    verbose: bool = False


def _import_tidy3d():
    """Import tidy3d lazily with a friendly error if the extra isn't installed."""
    try:
        import tidy3d as td

        return td
    except Exception as exc:  # pragma: no cover - exercised via the message
        raise RuntimeError(
            "The Tidy3D engine needs the tidy3d package. Install it with "
            '`pip install "phidler[tidy3d]"` (or `pip install tidy3d`).'
        ) from exc


def _tidy3d_structures(td, document, core_medium, thickness_um: float) -> list:
    """The waveguide structures for the tidy3d Simulation: one PolySlab per
    polygon of the core layer (full core height, centred on z=0) and of each
    configured etch/slab layer (partial height from the core bottom). The
    cladding isn't a structure — it's the Simulation's background medium, so
    everywhere outside a waveguide reads as cladding. Polygon holes are ignored
    (the outer hull only); photonic waveguides rarely have any."""
    settings = document.project_settings
    index_of = _layer_index_map(document.top)
    dbu = document.top.kcl.dbu
    polys = document.top.get_polygons()
    half_t = thickness_um / 2.0
    structures: list = []

    def add_layer(kdb_idx, z_lo, z_hi):
        for poly in polys.get(kdb_idx, []):
            verts = [(pt.x * dbu, pt.y * dbu) for pt in poly.each_point_hull()]
            if len(verts) >= 3:
                structures.append(
                    td.Structure(
                        geometry=td.PolySlab(vertices=verts, slab_bounds=(z_lo, z_hi), axis=2),
                        medium=core_medium,
                    )
                )

    core_idx = index_of.get(_CORE_LAYER)
    if core_idx is not None:
        add_layer(core_idx, -half_t, half_t)

    for etch in settings.etch_layers:
        slab = min(max(etch.slab_thickness_um, 0.0), settings.thickness_um)
        if slab <= 0.0:
            continue
        idx = index_of.get((etch.layer, etch.datatype))
        if idx is not None:
            add_layer(idx, -half_t, -half_t + slab)  # slab shares the core bottom
    return structures


def _tidy3d_source(td, spec: SourceSpec):
    """Map one placed SourceSpec to a tidy3d source. Only the always-available
    "dipole" is wired up (an Ez point dipole, matching the photonfdtd default);
    mode-matched / scripted / cherenkov sources raise a clear error rather than
    silently launching something different."""
    if spec.kind != "dipole":
        raise RuntimeError(
            f"The Tidy3D engine currently supports 'dipole' sources; "
            f"'{spec.kind}' isn't wired up on this engine yet."
        )
    freq0 = td.C_0 / spec.wavelength_um  # C_0 is µm·Hz, so this is Hz
    # tidy3d's GaussianPulse envelope has time std = 1/(2*pi*fwidth); invert the
    # time FWHM the user set (fwhm = 2*sqrt(2*ln2)*std) to that frequency width.
    fwidth = math.sqrt(2.0 * math.log(2.0)) / (math.pi * spec.fwhm_fs * 1e-15)
    return td.PointDipole(
        center=(spec.x_um, spec.y_um, 0.0),
        polarization="Ez",
        source_time=td.GaussianPulse(freq0=freq0, fwidth=fwidth),
    )


def build_tidy3d_simulation(
    document,
    params: FdtdParams = FdtdParams(),
    region_um: tuple[float, float, float, float] | None = None,
    cfg: Tidy3dConfig | None = None,
):
    """Build a ``tidy3d.Simulation`` from the placed layout — the tidy3d analogue
    of fdtd_sim.build_simulation. The core sits centred on z=0 (so the z=0 field
    plane the movie shows is mid-core), cladding is the background medium, the
    excitation is the document's sources (a default left-edge dipole if none),
    and a FieldTimeMonitor records the Ez movie on the z=0 plane."""
    td = _import_tidy3d()
    cfg = cfg or Tidy3dConfig()
    settings = document.project_settings

    left, bottom, right, top = region_um if region_um is not None else _layout_bbox_um_tuple(document)
    clad_n = params.clad_index if params.clad_index is not None else settings.clad_index
    core = td.Medium(permittivity=settings.core_index ** 2, name=settings.platform_name)
    clad = td.Medium(permittivity=clad_n ** 2, name="cladding")
    thickness = settings.thickness_um
    clad_th = fdtd_clad_thickness_um(settings, params.wavelength_um)
    pad = params.padding_um

    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    size_x = (right - left) + 2.0 * pad
    size_y = (top - bottom) + 2.0 * pad
    size_z = (thickness + 2.0 * clad_th) + 2.0 * pad

    structures = _tidy3d_structures(td, document, core, thickness)
    source_specs = params.sources or (
        SourceSpec(x_um=left, y_um=cy, wavelength_um=params.wavelength_um, fwhm_fs=params.pulse_fwhm_fs),
    )
    sources = [_tidy3d_source(td, s) for s in source_specs]

    ds = max(1, params.monitor_downsample)
    monitor = td.FieldTimeMonitor(
        center=(cx, cy, 0.0),
        size=(size_x, size_y, 0.0),
        fields=["Ez"],
        name="field",
        interval=max(1, params.monitor_interval),
        interval_space=(ds, ds, 1),
    )

    if cfg.use_cell_size:
        grid_spec = td.GridSpec.uniform(dl=params.resolved_cell_size_um())
    else:
        grid_spec = td.GridSpec.auto(
            min_steps_per_wvl=cfg.min_steps_per_wvl, wavelength=params.wavelength_um
        )

    return td.Simulation(
        center=(cx, cy, 0.0),
        size=(size_x, size_y, size_z),
        medium=clad,
        structures=structures,
        sources=sources,
        monitors=[monitor],
        grid_spec=grid_spec,
        boundary_spec=td.BoundarySpec.all_sides(td.PML()),
        run_time=params.resolved_run_time_fs() * 1e-15,
    )


def _result_stubs(sim, sim_data, elapsed: float) -> tuple[Any, Any, float]:
    """Convert a tidy3d SimulationData into the ``(sim_stub, result_stub,
    elapsed)`` the FDTD window's display reads — the same shape the photonfdtd
    transports return. ``result_stub.fields['field']['Ez']`` is the (frames, ny,
    nx) time movie; ``sim_stub.grid.coords`` / ``.shape`` label the axes."""
    import numpy as np

    ez = sim_data["field"].Ez  # dims (x, y, z, t)
    ez_plane = ez.isel(z=0)  # single z=0 plane -> (x, y, t)
    movie = np.asarray(ez_plane.transpose("t", "y", "x").values.real, dtype=np.float32)
    x = np.asarray(ez_plane.coords["x"].values, dtype=float)
    y = np.asarray(ez_plane.coords["y"].values, dtype=float)
    nx, ny, nz = (int(n) for n in sim.grid.num_cells)
    grid = SimpleNamespace(coords=[x, y, np.asarray([0.0])], shape=(nx, ny, nz))
    # engine="tidy3d" lets the window label the backend; use_gpu/use_numba are the
    # photonfdtd flags the shared result handler otherwise reads.
    sim_stub = SimpleNamespace(grid=grid, use_gpu=False, use_numba=False, engine="tidy3d")
    result_stub = SimpleNamespace(fields={"field": {"Ez": movie}})
    return sim_stub, result_stub, elapsed


def run_on_tidy3d(
    document,
    params: FdtdParams,
    region_um: tuple[float, float, float, float] | None = None,
    cfg: Tidy3dConfig | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[Any, Any, float]:
    """Build the simulation, run it on Tidy3D's cloud, and return the result in
    the ``(sim_stub, result_stub, elapsed)`` shape the FDTD window already reads.

    The cloud solve itself has no step-by-step callback, so ``progress_callback``
    is driven from the upload (0–40%) and download (60–100%) phases; the bar sits
    while the job queues/runs on the cloud. Raises RuntimeError on any Tidy3D
    error (missing key, rejected simulation, etc.)."""
    td = _import_tidy3d()
    import tidy3d.web as web

    cfg = cfg or Tidy3dConfig()
    sim = build_tidy3d_simulation(document, params, region_um, cfg)

    def _up(frac: float) -> None:
        if progress_callback is not None:
            progress_callback(int(frac * 40), 100)

    def _down(frac: float) -> None:
        if progress_callback is not None:
            progress_callback(60 + int(frac * 40), 100)

    t0 = time.time()
    try:
        sim_data = web.run(
            sim, task_name=cfg.task_name, verbose=cfg.verbose,
            progress_callback_upload=_up, progress_callback_download=_down,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the FDTD window
        raise RuntimeError(f"Tidy3D run failed: {exc}") from exc
    elapsed = time.time() - t0
    return _result_stubs(sim, sim_data, elapsed)
