from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .model.document import DEFAULT_WAVELENGTH_UM, LayoutDocument, ProjectSettings

logger = logging.getLogger(__name__)

# GDS (layer, datatype) of the full-height waveguide core. Etch/slab layers are
# configured per-project in ProjectSettings.etch_layers; this one is always the
# core. (Looked up to a kdb layer *index* per component — see _layer_index_map.)
_CORE_LAYER = (1, 0)

DISCLAIMER = (
    "This runs a real local FDTD solve (photonfdtd, a Yee-grid time-domain "
    "engine) against your actual placed layout — not a mockup — but it is "
    "an illustrative field visualization, not a calibrated transmission "
    "measurement. A plain point-dipole source excites radiation and "
    "reflections alongside whatever guided mode exists; a mode-injected "
    "source is closer to a real launch but the underlying mode solver is "
    "scalar (no TE/TM distinction) and the 'single photon' framing is "
    "semi-classical — a classical wavepacket normalized to approximately "
    "carry h·f of energy (the underlying library's own normalization is "
    "approximate, confirmed offset from the exact value here), not an "
    "actual quantum simulation. Multiple-photon counts scale the energy "
    "correctly relative to each other (confirmed N-fold for N photons) "
    "even though the absolute one-photon baseline isn't exactly h·f. "
    "Treat results as a qualitative look at how light spreads through "
    "your structure, the same spirit as the waveguide-width estimate "
    "elsewhere in this app — not a substitute for a calibrated photonic "
    "simulation tool."
)

# Measured directly on dev hardware across three scales (141k cells/394
# steps -> 2.5s; 525k cells/1312 steps -> 40.8s), NumPy backend, no GPU/
# numba. Cost scales close to linearly with cells*timesteps. This is a
# rough estimate for warning users before a run, not a guarantee — actual
# time depends on the machine.
_SECONDS_PER_CELL_STEP = 6e-8

# Speedup of each backend over the plain-NumPy baseline above, measured on dev
# hardware (a 5.2M-cell run: NumPy 32.6s, Numba 6.0s, GPU 2.4s compute). GPU
# also pays a fixed subprocess startup. All rough — they only feed the
# pre-flight warning, never correctness.
_BACKEND_SPEEDUP = {"numpy": 1.0, "numba": 5.5, "gpu": 13.5}
_GPU_STARTUP_SECONDS = 1.0

# Peak working-set memory per grid cell for the solve (6 field components + eps
# + update coefficients + CPML + rasterisation/temporaries, all float32).
# Measured from peak RSS slope: ~93 B/cell across 1.1M->6.4M cells; rounded up.
# This is what makes a big grid run out of memory, so it's worth showing.
_SOLVE_BYTES_PER_CELL = 100

PLANCK_H = 6.62607015e-34  # J*s, exact (SI 2019)
EV_TO_JOULE = 1.602176634e-19  # exact (SI 2019)
_C0 = 299792458.0  # m/s, exact


# photonfdtd isn't on PyPI, so it's fetched from its GitHub checkout — both by
# the SSH-offload deploy and by the app's on-demand "install it now?" prompt.
PHOTONFDTD_GIT_URL = "git+https://github.com/ngpaladi/photonfdtd.git"


class FdtdNotAvailableError(ImportError):
    """photonfdtd (and/or matplotlib) isn't installed. See pyproject.toml's
    `fdtd` extras group docstring — it's not yet on PyPI, so it must be
    installed from its own checkout first."""


def _import_photonfdtd():
    try:
        import photonfdtd as pf
    except ImportError as exc:
        raise FdtdNotAvailableError(
            "photonfdtd is not installed. It's not yet published on PyPI — "
            "install it from its own checkout first, e.g.:\n"
            "  pip install -e /path/to/photonfdtd\n"
            "then `pip install -e \".[fdtd]\"` here."
        ) from exc
    return pf


def photonfdtd_available() -> bool:
    """Whether the photonfdtd solver package can be imported. It's the one FDTD
    dependency not on PyPI (installed from its GitHub checkout), so when it's
    missing the app can offer to fetch it on demand rather than dead-end the
    Simulate action. Re-checks the live import each call (so it flips to True
    right after an in-app install), so call importlib.invalidate_caches() first
    if a fresh install may have landed in this same process."""
    try:
        import photonfdtd  # noqa: F401
        return True
    except ImportError:
        return False


def gpu_available() -> bool:
    """Whether photonfdtd's GPU backend can actually run here — it uses CuPy,
    so this is True only when CuPy imports. photonfdtd silently falls back to
    NumPy when it can't (use_gpu is AND'd with availability), so without this
    check a requested GPU run would quietly execute on the CPU. (CuPy importing
    doesn't by itself guarantee a working device, but it's the same signal
    photonfdtd keys off.)

    Backend-agnostic: this is True for CuPy's CUDA build (NVIDIA) *or* its
    ROCm/HIP build (AMD) — photonfdtd only uses generic CuPy array ops, so
    either drives the solve. See gpu_backend_name() for which one is live."""
    try:
        import cupy  # noqa: F401
        return True
    except Exception:
        return False


def gpu_backend_name() -> str | None:
    """Which CuPy GPU backend is active — "CUDA" (NVIDIA), "ROCm" (AMD), or None
    when no CuPy is importable ("GPU" as a last resort if CuPy is present but the
    build can't be identified). Used to tell the user *which* accelerator a run
    actually used, since phidler supports both through the same code path."""
    try:
        import cupy
    except Exception:
        return None
    try:
        # CuPy's ROCm build sets cupy.cuda.runtime.is_hip; the CUDA build leaves
        # it False/absent. getattr-guarded so an unexpected CuPy layout can't
        # raise here.
        if getattr(cupy.cuda.runtime, "is_hip", False):
            return "ROCm"
        return "CUDA"
    except Exception:
        return "GPU"


def numba_available() -> bool:
    """Whether photonfdtd's Numba JIT backend can run here (numba importable).
    Same silent-fallback caveat as gpu_available()."""
    try:
        import numba  # noqa: F401
        return True
    except Exception:
        return False


def wavelength_um_from_photon_energy_ev(energy_ev: float) -> float:
    """E = hc/lambda -> lambda = hc/E."""
    if energy_ev <= 0:
        raise ValueError("photon energy must be positive")
    energy_j = energy_ev * EV_TO_JOULE
    return (PLANCK_H * _C0 / energy_j) * 1e6


def photon_energy_ev_from_wavelength_um(wavelength_um: float) -> float:
    if wavelength_um <= 0:
        raise ValueError("wavelength_um must be positive")
    energy_j = PLANCK_H * _C0 / (wavelength_um * 1e-6)
    return energy_j / EV_TO_JOULE


@dataclass(frozen=True)
class SourceSpec:
    """One placed excitation. `kind="dipole"` is always available (a plain
    oscillating point source, not mode-matched to anything). `kind=
    "single_photon"` solves the local guided mode at `core_width_um` (the
    cross-section directly under the source position) and launches a real
    photonfdtd SinglePhotonSource built from that profile — needs
    core_width_um; raises if it's missing rather than guessing a width.
    `kind="scripted"` ignores wavelength_um/photon_count/core_width_um and
    instead evaluates `script` (a Python expression of `t`, in seconds) as
    the source's time-domain waveform — needs `script`.

    `kind="cherenkov"` models a charged particle punching up through the chip,
    perpendicular to the layout plane (out of the top-down view): a line of
    point dipoles along the +z track, each fired with a delay equal to the
    particle's transit time to that point (distance / (beta·c)). Their
    superposition forms the Cherenkov shock cone (opening at
    cos(theta)=1/(beta·n)), seen top-down as a ring spreading from the (x, y)
    impact point. Uses velocity_beta (v/c) and direction_deg (tilt from
    vertical); the track is kept inside the dielectric stack. cherenkov_segments
    sets how finely it's sampled."""

    x_um: float
    y_um: float
    kind: str = "dipole"  # "dipole" | "single_photon" | "scripted" | "cherenkov"
    wavelength_um: float = DEFAULT_WAVELENGTH_UM
    photon_count: int = 1
    core_width_um: float | None = None
    fwhm_fs: float = 3.0
    script: str | None = None
    velocity_beta: float = 0.8  # particle speed as a fraction of c (Cherenkov needs beta·n > 1)
    direction_deg: float = 0.0  # tilt of the +z track from vertical (0 = straight up, out of plane)
    cherenkov_length_um: float = 5.0  # track length in z (clamped to the dielectric stack)
    cherenkov_segments: int = 24  # point dipoles sampled along the track


class ScriptedWaveform:
    """Wraps a user-supplied Python expression of `t` (seconds, a NumPy
    array) into a callable waveform, e.g.
    "np.sin(2*np.pi*1.93e14*t) * np.exp(-((t-5e-15)/2e-15)**2)". Evaluated
    with eval() and no restricted namespace — the same trust model the
    scripting console already uses elsewhere in this app (a single-user
    desktop tool, not a new security boundary), not a sandboxed subset."""

    def __init__(self, script: str) -> None:
        self.script = script

    def __call__(self, t):
        import numpy as np

        return eval(self.script, {"np": np, "pi": np.pi}, {"t": np.asarray(t)})


@dataclass(frozen=True)
class FdtdParams:
    wavelength_um: float = DEFAULT_WAVELENGTH_UM
    cell_size_um: float | None = None  # defaults to wavelength_um / 15
    run_time_fs: float | None = None  # defaults to a few pulse widths
    padding_um: float = 0.5
    pulse_fwhm_fs: float = 3.0
    monitor_interval: int = 4
    # Empty -> build_simulation falls back to one default dipole source at
    # the layout's left edge, vertically centered (the original v1 default).
    sources: tuple[SourceSpec, ...] = ()
    # Override the project's cladding index (e.g. from a UI dropdown).
    # None means use document.project_settings.clad_index.
    clad_index: float | None = None
    # Optional acceleration backends passed through to photonfdtd. Both need
    # their own extra dependency (a CUDA-capable torch for GPU, numba for the
    # JIT path); leaving them False keeps the plain NumPy engine. A run with an
    # unavailable backend surfaces photonfdtd's own error via the worker.
    use_gpu: bool = False
    use_numba: bool = False
    # Field precision. float32 halves the field/CPML/monitor memory and is
    # faster, at single-precision accuracy — more than enough for the
    # qualitative field movie this app shows. float64 is bit-for-bit the old
    # behavior if a run ever needs it.
    precision: str = "float32"
    # Out-of-core (disk-streamed) tiled stepping (photonfdtd >=0.3): the full
    # field/CPML arrays live in memmapped scratch files and the domain is
    # stepped a slab at a time, so peak RAM is bounded by a tile instead of the
    # whole grid — the way to run a grid too big for RAM (it trades RAM for disk
    # and speed). NumPy backend only, so it overrides use_gpu/use_numba.
    out_of_core: bool = False
    # Spatial stride for the recorded field movie: keep every Nth cell on each
    # axis, cutting stored-movie memory by downsample**2 in the z=0 plane. 1 is
    # full resolution; 2–4 is plenty for the qualitative movie on a big region.
    monitor_downsample: int = 1

    def resolved_cell_size_um(self) -> float:
        # λ/15 is a deliberately coarse default — fewer cells (so faster, less
        # memory) for the qualitative field movie this tool produces. λ/20 is
        # finer/standard; the Cell size control lets a run go either way.
        return self.cell_size_um if self.cell_size_um is not None else self.wavelength_um / 15

    def resolved_run_time_fs(self) -> float:
        return self.run_time_fs if self.run_time_fs is not None else self.pulse_fwhm_fs * 8


@dataclass(frozen=True)
class SimulationConfig:
    """The user-editable simulation settings persisted with a project (saved
    in the .phidler file alongside project_settings). Captures what the FDTD
    window's two tabs hold — the propagation run parameters, the placed
    excitation sources, and the vertical-mode-profile inputs — so reopening a
    project restores the simulation you set up, especially the placed sources
    (tedious to re-drop on the canvas by hand).

    Stored as plain numbers/strings (no live gdsfactory/Qt objects), so it
    round-trips through JSON the same way the rest of the project does. A
    document whose simulation_config is None was simply never configured: the
    FDTD window then falls back to its project-settings-seeded defaults rather
    than overwriting them with this dataclass's placeholders."""

    # Propagation (FDTD) tab
    wavelength_um: float = DEFAULT_WAVELENGTH_UM
    cell_size_um: float | None = None  # None -> FdtdParams' λ/15 default
    run_time_fs: float | None = None  # None -> FdtdParams' pulse-width default
    clad_index: float | None = None  # None -> project_settings.clad_index
    use_gpu: bool = False
    use_numba: bool = False
    region_selected_only: bool = False
    sources: tuple[SourceSpec, ...] = ()

    # Vertical mode profile tab
    mode_wavelength_um: float = DEFAULT_WAVELENGTH_UM
    mode_core_width_um: float = 0.5
    mode_num_modes: int = 1
    mode_clad_index: float | None = None  # None -> project_settings.clad_index


@dataclass(frozen=True)
class ModeProfileParams:
    wavelength_um: float = DEFAULT_WAVELENGTH_UM
    core_width_um: float = 0.5
    cell_size_um: float = 0.02
    num_modes: int = 1
    lateral_padding_factor: float = 2.0  # extra domain width on each side, in wavelengths
    # Override the project's cladding index (e.g. from a UI dropdown).
    # None means use settings.clad_index.
    clad_index: float | None = None


@dataclass(frozen=True)
class ConfinementCheck:
    edge_to_peak_ratio: float
    well_confined: bool  # True if edge_to_peak_ratio is small (mode hasn't hit the domain boundary)
    infinite_clad: bool = False  # solved with the "assume infinite cladding depth" extent

    @property
    def message(self) -> str:
        if self.well_confined:
            return f"Well confined (edge/peak amplitude {self.edge_to_peak_ratio:.1%})"
        if self.infinite_clad:
            # The thickness control is ignored in infinite mode, so don't tell
            # the user to raise it — a leftover edge amplitude here means the
            # mode is weakly guided (n_eff near the cladding index), not thin.
            return (
                f"Mode is weakly guided — non-trivial amplitude remains at the "
                f"domain edge (edge/peak {self.edge_to_peak_ratio:.1%}) even with "
                "infinite cladding depth."
            )
        return (
            f"Cladding may be too thin — mode is truncated at the domain "
            f"edge (edge/peak amplitude {self.edge_to_peak_ratio:.1%}). "
            "Try increasing cladding thickness in Project Settings."
        )


_CONFINEMENT_WELL_CONFINED_THRESHOLD = 0.01

# When ProjectSettings.clad_infinite is set, the cladding extent is replaced by
# this many wavelengths on each side of the core. Six wavelengths is enough for
# a guided mode's evanescent tail to decay to a negligible amplitude for the
# index contrasts this app's platforms span (Si, SiN, LN, LT), so the finite
# domain behaves as semi-infinite without an unbounded z-grid.
_INFINITE_CLAD_WAVELENGTHS = 6.0


def effective_clad_thickness_um(settings: ProjectSettings, wavelength_um: float) -> float:
    """Cladding half-extent (each side of the core) the mode solver should use.

    Normally the project's chosen clad_thickness_um. When clad_infinite is set,
    a wavelength-scaled extent large enough that the mode no longer interacts
    with the domain boundary — the "assume infinite cladding depth" mode."""
    if settings.clad_infinite:
        return _INFINITE_CLAD_WAVELENGTHS * wavelength_um
    return settings.clad_thickness_um


# How much cladding the 3D *propagation* domain keeps on each side of the core.
# The mode solver may want a very thick (even "infinite", ~6λ) cladding so the
# mode doesn't truncate, but FDTD propagation only needs enough to hold the
# evanescent field — every extra micron of cladding is dead z-grid that costs
# runtime and recorded-movie memory for nothing. So the domain keeps just a few
# evanescent decay lengths of cladding (which scales with the index contrast:
# tightly-confined high-contrast platforms like Si need far less than weakly-
# guided ones), clamped to a floor and a hard ceiling. Verified empirically: the
# displayed in-plane (z=0) field is unchanged from the old fixed 2µm down to
# 0.5µm for both Si and SiN.
_FDTD_CLAD_CAP_UM = 2.0  # hard ceiling, regardless of contrast
_FDTD_CLAD_MIN_UM = 0.5  # floor — never thinner than this
_FDTD_CLAD_DECAY_LENGTHS = 6.0  # keep this many evanescent decay lengths of cladding


def fdtd_clad_thickness_um(settings: ProjectSettings, wavelength_um: float) -> float:
    """Cladding half-extent for the 3D FDTD propagation domain: a few evanescent
    decay lengths (set by the core/cladding index contrast), clamped to
    [_FDTD_CLAD_MIN_UM, _FDTD_CLAD_CAP_UM] and never more than the mode solver's
    extent. Much thinner than the mode-solver cladding for high-contrast
    platforms, which keeps the z grid (and the run) small.

    Note: a cherenkov source's dipole track spans this domain, so a thinner
    domain also shortens that track — fine for the qualitative field movie, but
    why the cladding controls still feed the (full-extent) mode solver."""
    full = effective_clad_thickness_um(settings, wavelength_um)
    if settings.core_index > settings.clad_index:
        decay_um = wavelength_um / (2 * math.pi * math.sqrt(settings.core_index**2 - settings.clad_index**2))
        needed = _FDTD_CLAD_DECAY_LENGTHS * decay_um
    else:
        needed = _FDTD_CLAD_CAP_UM
    return min(full, max(needed, _FDTD_CLAD_MIN_UM), _FDTD_CLAD_CAP_UM)


def _layout_bbox_um(document: LayoutDocument):
    bbox = document.top.bbox()
    if bbox.empty():
        raise ValueError("Cannot run an FDTD simulation on an empty layout — place at least one component first.")
    return bbox


def _layout_bbox_um_tuple(document: LayoutDocument) -> tuple[float, float, float, float]:
    """The layout bounding box as (left, bottom, right, top) in µm."""
    bbox = _layout_bbox_um(document)
    return float(bbox.left), float(bbox.bottom), float(bbox.right), float(bbox.top)


def estimate_grid_cell_count(
    document: LayoutDocument,
    params: FdtdParams = FdtdParams(),
    region_um: tuple[float, float, float, float] | None = None,
) -> int:
    """Cheap pre-flight check: builds only the Grid (no structures, no
    source, no monitor, no run) to estimate the total cell count a real
    run would use, so a caller can warn before committing to an expensive
    simulation. True 3D now (no z-collapse), so this includes the full
    core+cladding vertical extent. region_um (left, bottom, right, top in µm)
    restricts the xy window to simulate, else the whole layout is used."""
    pf = _import_photonfdtd()
    left, bottom, right, top = region_um if region_um is not None else _layout_bbox_um_tuple(document)
    settings = document.project_settings
    cell_size_m = params.resolved_cell_size_um() * 1e-6
    padding_m = params.padding_um * 1e-6
    x_extent = (right - left) * 1e-6 + 2 * padding_m
    y_extent = (top - bottom) * 1e-6 + 2 * padding_m
    clad_thickness_um = fdtd_clad_thickness_um(settings, params.wavelength_um)
    z_extent = settings.thickness_um * 1e-6 + 2 * clad_thickness_um * 1e-6 + 2 * padding_m
    grid = pf.Grid(size=(x_extent, y_extent, z_extent), cell_size=cell_size_m, pml_layers=(12, 12, 12))
    return int(grid.shape[0]) * int(grid.shape[1]) * int(grid.shape[2])


def estimate_run_seconds(grid_shape: tuple[int, int, int], n_steps: int, backend: str = "numpy") -> float:
    """Rough, empirically-calibrated estimate (see _SECONDS_PER_CELL_STEP's
    docstring) — not a guarantee, just enough to warn before a run that
    would otherwise look like a frozen app. `backend` is one of "numpy",
    "numba", "gpu"; GPU also includes its fixed subprocess startup."""
    total_cells = int(grid_shape[0]) * int(grid_shape[1]) * int(grid_shape[2])
    seconds = total_cells * int(n_steps) * _SECONDS_PER_CELL_STEP / _BACKEND_SPEEDUP.get(backend, 1.0)
    if backend == "gpu":
        seconds += _GPU_STARTUP_SECONDS
    return seconds


def estimate_memory_gb(cell_count: int) -> float:
    """Rough peak working-set memory (GB) the solve needs for a grid of this
    many cells — the thing that makes a big grid run out of memory. Scales with
    cells (see _SOLVE_BYTES_PER_CELL); independent of timesteps."""
    return int(cell_count) * _SOLVE_BYTES_PER_CELL / 1e9


# Out-of-core on-disk working set per cell: the six field components + the
# update coefficient field + the CPML psi arrays, all streamed to memmapped
# scratch files at float32. Rounded up from the arrays run_out_of_core creates.
_OOC_DISK_BYTES_PER_CELL = 56
# Leave headroom below total RAM (and free disk) for the OS, the GUI process,
# Python/gdsfactory overhead, and allocation spikes the flat per-cell estimate
# doesn't capture. A run estimated above this fraction is refused up front.
_MEMORY_SAFETY_FRACTION = 0.8


def total_ram_gb() -> float:
    """Total physical RAM in GB, or 0.0 if it can't be determined (then the
    feasibility guard is skipped rather than guessing)."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except (ValueError, AttributeError, OSError):
        return 0.0


def estimate_out_of_core_disk_gb(cell_count: int) -> float:
    """Scratch disk (GB) an out-of-core run needs for its memmapped field/CPML
    arrays — bounded by the grid size, not the timestep count."""
    return int(cell_count) * _OOC_DISK_BYTES_PER_CELL / 1e9


def check_run_feasible(
    cell_count: int, params: FdtdParams, workdir: str | None = None
) -> None:
    """Raise RuntimeError (with actionable guidance) if a run this large can't
    fit — called *before* from_gdsfactory allocates the full grid, so an
    over-large layout fails fast with a clear message instead of thrashing swap
    and freezing the machine. In-core runs are checked against RAM; out-of-core
    runs against free scratch disk (their peak RAM is a bounded tile). If the
    machine size can't be read (total_ram_gb()/disk == 0) the check is skipped."""
    if params.out_of_core:
        need_disk = estimate_out_of_core_disk_gb(cell_count)
        try:
            free_disk = shutil.disk_usage(workdir or tempfile.gettempdir()).free / 1e9
        except OSError:
            free_disk = 0.0
        if free_disk and need_disk > free_disk * _MEMORY_SAFETY_FRACTION:
            raise RuntimeError(
                f"This out-of-core simulation needs about {need_disk:.0f} GB of scratch "
                f"disk for its {cell_count:,}-cell grid, but only {free_disk:.0f} GB is "
                "free. Simulate a smaller selected region or use a coarser cell size."
            )
        return
    need_ram = estimate_memory_gb(cell_count)
    ram = total_ram_gb()
    if ram and need_ram > ram * _MEMORY_SAFETY_FRACTION:
        raise RuntimeError(
            f"This simulation needs about {need_ram:.0f} GB of memory for its "
            f"{cell_count:,}-cell grid, but this machine has {ram:.0f} GB. Simulate a "
            "smaller selected region, use a coarser cell size, or enable out-of-core "
            "(disk-streamed) stepping for a grid too big for RAM."
        )


def feasible_cell_budget(
    params: FdtdParams, workdir: str | None = None, target_fraction: float = 0.6
) -> int:
    """The largest grid (in cells) that comfortably fits this machine for the
    run's mode: RAM for an in-core run, free scratch disk for out-of-core. Kept
    below check_run_feasible's refusal ceiling (target_fraction < the guard's
    safety fraction) so a region sized to this budget actually runs. Returns a
    large fallback if the machine size can't be read, so suggestions still work."""
    if params.out_of_core:
        try:
            free = shutil.disk_usage(workdir or tempfile.gettempdir()).free / 1e9
        except OSError:
            free = 0.0
        per_cell = _OOC_DISK_BYTES_PER_CELL
        capacity = free
    else:
        capacity = total_ram_gb()
        per_cell = _SOLVE_BYTES_PER_CELL
    if not capacity:
        return 200_000_000  # unknown machine: a sane, runnable default
    return max(1, int(capacity * target_fraction * 1e9 / per_cell))


def _default_region_center_um(document: LayoutDocument, params: FdtdParams) -> tuple[float, float]:
    """Where a suggested region should sit: the sources' centroid (the physics
    of interest), or the layout centre if no sources are placed yet."""
    xs = [s.x_um for s in params.sources]
    ys = [s.y_um for s in params.sources]
    if xs:
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    left, bottom, right, top = _layout_bbox_um_tuple(document)
    return ((left + right) / 2.0, (bottom + top) / 2.0)


def suggest_region_um(
    document: LayoutDocument,
    params: FdtdParams,
    max_cells: int,
    center_um: tuple[float, float] | None = None,
) -> tuple[float, float, float, float]:
    """A runnable square xy region (left, bottom, right, top in µm) centred on
    ``center_um`` (default: the sources' centroid, see _default_region_center_um)
    whose grid fits within ``max_cells`` — the way to turn "the whole chip is too
    big" into a look at the interesting part. Shrinks by the grid's area ratio
    until it fits (cells scale with the region's area, z fixed)."""
    cx, cy = center_um if center_um is not None else _default_region_center_um(document, params)
    cell_um = params.resolved_cell_size_um()
    side = max(cell_um * 8.0, (max_cells ** 0.5) * cell_um)  # generous start, then shrink to fit
    region = (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)
    for _ in range(60):
        half = side / 2.0
        region = (cx - half, cy - half, cx + half, cy + half)
        cells = estimate_grid_cell_count(document, params, region_um=region)
        if cells <= max_cells:
            break
        side *= max(0.5, (max_cells / cells) ** 0.5 * 0.98)  # 0.98 margin so it lands under, not on
    return region


def build_mode_solver(settings: ProjectSettings, params: ModeProfileParams = ModeProfileParams()) -> Any:
    """Builds a 2D cross-sectional mode solver for the platform's vertical
    stack (core centered at z=0, i.e. z=0 means mid-core-height — the same
    convention build_simulation's 3D domain uses, so a solved profile can
    be dropped straight into a SinglePhotonSource without extra offset
    bookkeeping) using a real, finite cladding thickness — unlike the
    waveguide-width EIM estimate elsewhere in this app, which assumes
    semi-infinite cladding, this is the tool that makes "cladding
    thickness" a parameter that actually matters: too-thin cladding
    visibly truncates the mode against the solver's zero-boundary
    (confirmed empirically before this was built)."""
    pf = _import_photonfdtd()
    core = pf.Medium.from_index(settings.core_index, name=settings.platform_name)
    clad_n = params.clad_index if params.clad_index is not None else settings.clad_index
    clad = pf.Medium.from_index(clad_n, name="cladding")

    thickness_m = settings.thickness_um * 1e-6
    clad_thickness_m = effective_clad_thickness_um(settings, params.wavelength_um) * 1e-6
    wavelength_m = params.wavelength_um * 1e-6
    cell_size_m = params.cell_size_um * 1e-6

    ly = (params.core_width_um + 2 * params.lateral_padding_factor * params.wavelength_um) * 1e-6
    lz = thickness_m + 2 * clad_thickness_m

    core_box = pf.Box(
        center=(0.0, 0.0),
        size=(params.core_width_um * 1e-6, thickness_m),
        medium=core,
    )
    structures = [core_box]

    # Rib waveguide: a slab of core material spanning the full domain width,
    # sharing the core's bottom (z in [-t/2, -t/2 + slab]). It raises the lateral
    # effective index, so the mode is a rib mode rather than a strip mode. The
    # cross-section is a single idealised profile, so the tallest configured slab
    # is used (settings.max_slab_thickness_um) as the dominant lateral guide.
    slab_um = settings.max_slab_thickness_um()
    if slab_um > 0.0:
        slab_m = min(slab_um, settings.thickness_um) * 1e-6
        structures.append(pf.Box(
            center=(0.0, -thickness_m / 2 + slab_m / 2),
            size=(ly, slab_m),
            medium=core,
        ))

    return pf.ModeSolver(
        size=(ly, lz),
        cell_size=cell_size_m,
        structures=structures,
        wavelength=wavelength_m,
        background_eps=clad.eps_r,
        num_modes=params.num_modes,
    )


def solve_mode_profile(solver: Any) -> Any:
    return solver.solve()


def mode_confinement(result: Any, mode_index: int = 0, infinite_clad: bool = False) -> ConfinementCheck:
    """How close the chosen mode's amplitude gets to the solve domain's
    zero-amplitude boundary — a too-thin cladding forces the mode to decay
    to zero before it naturally would, which shows up as a non-trivial
    edge amplitude relative to the peak (confirmed empirically: ~29% at a
    deliberately-too-thin 0.05um cladding vs ~0% once the cladding is
    thick enough)."""
    psi = result.psi[mode_index]
    edge_amp = max(float(abs(psi[:, 0]).max()), float(abs(psi[:, -1]).max()))
    peak_amp = float(abs(psi).max())
    ratio = edge_amp / peak_amp if peak_amp > 0 else 0.0
    return ConfinementCheck(
        edge_to_peak_ratio=ratio,
        well_confined=ratio < _CONFINEMENT_WELL_CONFINED_THRESHOLD,
        infinite_clad=infinite_clad,
    )


def build_source(settings: ProjectSettings, spec: SourceSpec) -> Any:
    """Builds the photonfdtd source object for one placed SourceSpec.

    "dipole": a plain oscillating point source at the chosen wavelength —
    always available, not mode-matched to anything (same honest framing
    as the original v1 default).

    "single_photon": solves the local guided mode at spec.core_width_um
    and builds a real photonfdtd.SinglePhotonSource from that profile —
    a ModeSource whose amplitude is normalised so the launched wavepacket
    carries approximately h*freq0 of energy (the library's own mechanism,
    not new physics code; its own docstring flags this normalisation as
    approximate and suggests verifying with a FluxMonitor — done below,
    and the *absolute* scale does come out offset from h*freq0, consistent
    with that caveat). For photon_count > 1, scales the *amplitude* by
    sqrt(photon_count), not by stacking N copies — N coherent copies at
    the same place/phase would add amplitude N-fold and therefore energy
    (which is proportional to amplitude squared) N^2-fold, overshooting by
    a factor of N. Verified numerically with a FluxMonitor (see
    tests/test_fdtd_sim.py) that sqrt(N) scaling gives a *relative* N-fold
    increase in integrated energy (confirmed exactly: 4x energy at
    photon_count=4, 9x at photon_count=9, relative to photon_count=1) —
    the relative N-scaling is correct even though the absolute per-photon
    calibration inherits the library's own approximation.

    "scripted": ignores wavelength_um/photon_count/core_width_um and
    evaluates spec.script (a Python expression of t, in seconds) as the
    waveform — see ScriptedWaveform.
    """
    pf = _import_photonfdtd()

    if spec.kind == "scripted":
        if not spec.script:
            raise ValueError("scripted sources need a script")
        return pf.PointDipole(
            position=(spec.x_um * 1e-6, spec.y_um * 1e-6, 0.0),
            component="Ez",
            waveform=ScriptedWaveform(spec.script),
        )

    freq0 = _C0 / (spec.wavelength_um * 1e-6)
    waveform = pf.GaussianPulse(freq0=freq0, fwhm=spec.fwhm_fs * 1e-15)

    if spec.kind == "cherenkov":
        if spec.velocity_beta <= 0:
            raise ValueError("cherenkov sources need a positive velocity_beta (v/c)")
        v = spec.velocity_beta * _C0  # particle speed (m/s)
        # The particle travels (mostly) along +z — up and out of the chip plane,
        # the way a real charged particle punches through the wafer — so the
        # top-down field view shows the Cherenkov shock as a ring spreading from
        # the (x, y) impact point. direction_deg tilts the track from vertical
        # (0 = straight through). The track is kept inside the dielectric stack
        # (core + cladding), both because that's where Cherenkov light is
        # actually emitted and so the dipoles never fall outside the domain.
        clad_m = fdtd_clad_thickness_um(settings, spec.wavelength_um) * 1e-6
        thickness_m = settings.thickness_um * 1e-6
        diel_span = thickness_m + 2 * clad_m
        z_center = thickness_m / 2.0  # mid-core; the dielectric runs [-clad, thickness+clad]
        span_m = min(spec.cherenkov_length_um * 1e-6, diel_span)
        tilt = math.radians(spec.direction_deg)
        ux, uy, uz = math.sin(tilt), 0.0, math.cos(tilt)
        n_seg = max(spec.cherenkov_segments, 2)
        step_m = span_m / (n_seg - 1)
        fwhm_s = spec.fwhm_fs * 1e-15
        x0 = spec.x_um * 1e-6 - ux * span_m / 2
        y0 = spec.y_um * 1e-6 - uy * span_m / 2
        z0 = z_center - uz * span_m / 2  # enter from below, exit above
        dipoles = []
        for i in range(n_seg):
            s = i * step_m  # distance travelled along the track
            pos = (x0 + ux * s, y0 + uy * s, z0 + uz * s)
            # Each point fires when the particle reaches it: delay = s / v.
            pulse = pf.GaussianPulse(freq0=freq0, fwhm=fwhm_s, delay=s / v)
            dipoles.append(pf.PointDipole(position=pos, component="Ez", waveform=pulse))
        return dipoles

    if spec.kind == "dipole":
        return pf.PointDipole(
            position=(spec.x_um * 1e-6, spec.y_um * 1e-6, 0.0),
            component="Ez",
            waveform=waveform,
        )

    if spec.kind == "single_photon":
        if spec.core_width_um is None:
            raise ValueError("single_photon sources need core_width_um to solve the local mode")
        mode_params = ModeProfileParams(
            wavelength_um=spec.wavelength_um,
            core_width_um=spec.core_width_um,
        )
        solver = build_mode_solver(settings, mode_params)
        result = solve_mode_profile(solver)

        single = pf.SinglePhotonSource(
            center=(spec.x_um * 1e-6, spec.y_um * 1e-6, 0.0),
            size=(0.0, result.y[-1] - result.y[0], result.z[-1] - result.z[0]),
            component="Ey",
            waveform=waveform,
            profile=result.psi[0],
            profile_coords=(result.y, result.z),
            n_eff=float(result.n_eff[0]),
        )
        if spec.photon_count == 1:
            return single
        scaled_amplitude = single.peak_field * math.sqrt(spec.photon_count) * (2.0 if single.bidirectional else 1.0)
        mode_source = single.as_mode_source()
        return pf.ModeSource(
            center=mode_source.center,
            size=mode_source.size,
            component=mode_source.component,
            waveform=mode_source.waveform,
            profile=mode_source.profile,
            profile_coords=mode_source.profile_coords,
            amplitude=scaled_amplitude,
        )

    raise ValueError(f"unknown source kind: {spec.kind!r}")


def nearest_z_index(grid: Any, z_um: float = 0.0) -> int:
    """Maps a vertical position (microns, in the same mid-core-centred
    frame build_simulation/build_mode_solver use) to the nearest grid
    index along z — used to slice a top-down field frame out of the full
    3D field array for the "money shot" plan view."""
    import numpy as np

    z_coords = grid.coords[2]
    return int(np.argmin(np.abs(z_coords - z_um * 1e-6)))


def _layer_index_map(component: Any) -> dict[tuple[int, int], int]:
    """Map ``(gds_layer, gds_datatype) -> kdb layer index`` for the layers that
    actually carry geometry in ``component``.

    photonfdtd's from_gdsfactory keys its ``layers`` map by the integer kdb
    layer *index* returned from ``component.get_polygons()``, which is NOT the
    GDS layer number — e.g. WG (1, 0) may be index 1 while SLAB (2, 0) is index
    3. So every layer we want to simulate has to be translated through this."""
    kcl = component.kcl
    out: dict[tuple[int, int], int] = {}
    for idx in component.get_polygons().keys():
        info = kcl.get_info(idx)
        out[(int(info.layer), int(info.datatype))] = idx
    return out


def build_layer_media_map(document: LayoutDocument, core: Any, thickness_m: float) -> dict[int, tuple[Any, tuple[float, float]]]:
    """The ``{kdb_index: (medium, (z_min_m, z_max_m))}`` map for from_gdsfactory:
    the full-height core, plus a partial-height core slab for each configured
    etch layer (rib/slab geometry). All share the core's bottom at z=0; the core
    spans (0, thickness) and a slab spans (0, slab_thickness), so a slab-only
    region reads core below the slab height and cladding above it.

    Etch layers that aren't actually drawn in the layout (wrong layer number, or
    a strip-only design) are skipped with a warning rather than silently doing
    nothing."""
    settings = document.project_settings
    index_of = _layer_index_map(document.top)

    layers_map: dict[int, tuple[Any, tuple[float, float]]] = {}
    core_idx = index_of.get(_CORE_LAYER)
    if core_idx is not None:
        layers_map[core_idx] = (core, (0.0, thickness_m))

    for etch in settings.etch_layers:
        # The slab is a partial etch: a remaining core height strictly inside the
        # full thickness. Clamp defensively and skip a no-op (<=0) slab.
        slab_m = min(max(etch.slab_thickness_um, 0.0), settings.thickness_um) * 1e-6
        if slab_m <= 0.0:
            continue
        idx = index_of.get((etch.layer, etch.datatype))
        if idx is None:
            logger.warning(
                "Etch layer (%d, %d) is configured but has no geometry in the "
                "layout — skipping it.", etch.layer, etch.datatype,
            )
            continue
        layers_map[idx] = (core, (0.0, slab_m))
    return layers_map


def build_simulation(
    document: LayoutDocument,
    params: FdtdParams = FdtdParams(),
    region_um: tuple[float, float, float, float] | None = None,
) -> Any:
    """Builds a photonfdtd Simulation from the document's actual placed
    layout, with sources and a field monitor already attached
    (from_gdsfactory itself returns sources=[]/monitors=[] per its own
    docstring — adding the excitation is the caller's job).

    True 3D (no z-collapse) — the vertical stack (core thickness + real,
    finite cladding thickness from ProjectSettings) is now genuinely
    resolved, unlike the original quasi-2D version of this function which
    forced z_size=0 for speed. That made the cladding-thickness setting
    inert; this version is why that setting now does something. Cost is
    real (calibrated empirically — see estimate_run_seconds) but no longer
    avoidable if the "money shot" top-down field movie is meant to come
    from an actual resolved simulation rather than a 2D approximation.

    Core/cladding both come from the document's own ProjectSettings — the
    same values the waveguide-width estimate and mode solver already use —
    so the platform picker (Silicon, SiN, LN, LT) drives this too.
    """
    pf = _import_photonfdtd()
    # Fail fast if the grid can't fit RAM (or scratch disk, out-of-core) — before
    # from_gdsfactory tries to allocate the full eps/field arrays and OOMs.
    check_run_feasible(estimate_grid_cell_count(document, params, region_um), params)
    left, bottom, right, top = region_um if region_um is not None else _layout_bbox_um_tuple(document)
    # An explicit region only grids that xy window (structures outside it are
    # clipped) — the way to keep a large layout's FDTD run from running out of
    # memory: simulate the part you care about, not the whole chip.
    xy_bounds = (left * 1e-6, right * 1e-6, bottom * 1e-6, top * 1e-6) if region_um is not None else None
    bbox = SimpleNamespace(left=left, bottom=bottom, right=right, top=top)
    settings = document.project_settings

    core = pf.Medium.from_index(settings.core_index, name=settings.platform_name)
    clad_n = params.clad_index if params.clad_index is not None else settings.clad_index
    clad = pf.Medium.from_index(clad_n, name="cladding")
    thickness_m = settings.thickness_um * 1e-6
    clad_thickness_m = fdtd_clad_thickness_um(settings, params.wavelength_um) * 1e-6
    cell_size_m = params.resolved_cell_size_um() * 1e-6
    padding_m = params.padding_um * 1e-6

    sim = pf.from_gdsfactory(
        document.top,
        # Full-height core plus any partial-height rib/slab etch layers. Keyed by
        # kdb layer index (translated from GDS layer/datatype) — see
        # build_layer_media_map / _layer_index_map.
        layers=build_layer_media_map(document, core, thickness_m),
        # Background slabs are stamped before the per-layer polygons (the
        # polygon wins where they overlap, per from_gdsfactory's own
        # documented order) — so the middle slab here, spanning the same
        # z-range as the core layer, is what makes everywhere *outside*
        # the waveguide footprint (but at core height) read as lateral
        # cladding instead of silently defaulting to vacuum (eps_r=1).
        # Missing this slab was a real bug, caught by actually inspecting
        # sim.eps_r at the core z-height before trusting this code.
        background_slabs=[
            (clad, (-clad_thickness_m, 0.0)),
            (clad, (0.0, thickness_m)),
            (clad, (thickness_m, thickness_m + clad_thickness_m)),
        ],
        cell_size=cell_size_m,
        padding=(padding_m, padding_m, padding_m),
        run_time=params.resolved_run_time_fs() * 1e-15,
        # Out-of-core stepping is NumPy-only, so it overrides the accel backends
        # (photonfdtd's run_out_of_core rejects a gpu/numba Simulation).
        use_gpu=params.use_gpu and not params.out_of_core,
        use_numba=params.use_numba and not params.out_of_core,
        precision=params.precision,
        xy_bounds=xy_bounds,
    )

    sources = params.sources
    if not sources:
        sources = (SourceSpec(x_um=bbox.left, y_um=(bbox.top + bbox.bottom) / 2, wavelength_um=params.wavelength_um, fwhm_fs=params.pulse_fwhm_fs),)
    for spec in sources:
        built = build_source(settings, spec)
        # A cherenkov spec expands into a whole track of point dipoles.
        for src in built if isinstance(built, list) else [built]:
            sim.add_source(src)

    # Record only what the field movie actually shows: the Ez component on the
    # single mid-core (z=0) plane. The display only ever reads Ez at z=0, so
    # recording Ey too and the whole z-volume was pure waste — this keeps the
    # stored movie ~(2 components x z-cell-count) smaller, which is what makes a
    # large run fit in memory (and shrinks the GPU host transfer to nothing).
    # z=0 is mid-core height: from_gdsfactory centres the stack, so the core
    # sits symmetrically across z=0 and the guided mode peaks there.
    sim.add_monitor(
        pf.FieldMonitor(
            name="field",
            components=("Ez",),
            interval=params.monitor_interval,
            plane_z=0.0,
            # Spatial stride shrinks the stored movie by downsample**2 (the z=0
            # plane is already 2D) — cheap way to keep a big region's movie small.
            downsample=params.monitor_downsample,
        )
    )

    return sim


# Logical cores to leave free for the rest of the machine while a solve runs.
# photonfdtd's numba kernel is @njit(parallel=True) and grabs one thread per
# logical core by default; on the in-process (CPU/numba) path that shares the
# GUI process, pinning *every* core starves the compositor and window manager
# and freezes the whole desktop for the run's duration — everything locks up
# but the (hardware) mouse cursor still moves. Reserving a couple of cores keeps
# the desktop (and, on the SSH/nereid path, the remote box) usable for a small,
# sub-linear hit to solve throughput. See panels.fdtd_window / fdtd_subprocess.
_SOLVER_RESERVED_CORES = 2


def limit_solver_threads(renice: bool = False) -> int:
    """Cap the numba solve to leave ``_SOLVER_RESERVED_CORES`` logical cores
    free so a CPU-bound run can't pin every core and freeze the machine. Returns
    the thread count applied (always >= 1).

    Call once before each solve on every path that runs numba locally to the
    executing machine — the in-process worker *and* the subprocess entry point
    (which is also what runs on the SSH remote host and the nereid server).
    ``numba.set_num_threads`` re-applies per run (numba only creates its pool
    once, so setting it at import time wouldn't stick across the app's lifetime).

    Best-effort: a no-op if numba isn't importable. ``renice`` (POSIX only, for
    the dedicated child process where lowering the whole process is safe) also
    drops the caller below interactive priority so the desktop wins any
    remaining CPU contention; harmless/ignored where unsupported."""
    n_threads = max(1, (os.cpu_count() or 2) - _SOLVER_RESERVED_CORES)
    try:
        import numba

        numba.set_num_threads(n_threads)
    except Exception:  # numba absent (plain-NumPy engine) or set_num_threads unavailable
        pass
    if renice:
        try:
            os.nice(10)  # only lowers priority; a discarded child never needs it restored
        except (AttributeError, OSError):  # no os.nice (Windows) or not permitted
            pass
    return n_threads


def run_simulation(sim: Any, out_of_core: bool = False, tile_cells: int | None = None) -> Any:
    """Trivial wrapper kept separate from build_simulation so a threading
    layer only has to call this one function — the actual compute, with
    no Qt/threading concerns mixed in, stays directly unit-testable.

    ``out_of_core`` streams the field/CPML arrays to disk and steps the domain
    in slabs so peak RAM is bounded by a tile (photonfdtd >=0.3); ``tile_cells``
    is the planes-per-tile knob (None lets photonfdtd pick grid.shape[0]//8)."""
    if out_of_core:
        return sim.run(out_of_core=True, tile_cells=tile_cells)
    return sim.run()
