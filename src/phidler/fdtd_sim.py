from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .model.document import LayoutDocument, ProjectSettings

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

PLANCK_H = 6.62607015e-34  # J*s, exact (SI 2019)
EV_TO_JOULE = 1.602176634e-19  # exact (SI 2019)
_C0 = 299792458.0  # m/s, exact


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
            "  pip install -e /path/to/FastTiming/photonfdtd\n"
            "then `pip install -e \".[fdtd]\"` here."
        ) from exc
    return pf


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

    `kind="cherenkov"` models a charged particle crossing the domain faster
    than light's local phase velocity: a line of point dipoles along a path,
    each fired with a delay equal to the particle's transit time to that
    point (distance / (beta·c)). Their superposition forms the Cherenkov
    shock cone, opening at cos(theta)=1/(beta·n). Uses velocity_beta (v/c),
    direction_deg, and cherenkov_length_um for the path; cherenkov_segments
    sets how finely the path is sampled."""

    x_um: float
    y_um: float
    kind: str = "dipole"  # "dipole" | "single_photon" | "scripted" | "cherenkov"
    wavelength_um: float = 1.55
    photon_count: int = 1
    core_width_um: float | None = None
    fwhm_fs: float = 3.0
    script: str | None = None
    velocity_beta: float = 0.8  # particle speed as a fraction of c (Cherenkov needs beta·n > 1)
    direction_deg: float = 0.0  # particle travel direction in the XY plane
    cherenkov_length_um: float = 5.0  # length of the particle track
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
    wavelength_um: float = 1.55
    cell_size_um: float | None = None  # defaults to wavelength_um / 20
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

    def resolved_cell_size_um(self) -> float:
        return self.cell_size_um if self.cell_size_um is not None else self.wavelength_um / 20

    def resolved_run_time_fs(self) -> float:
        return self.run_time_fs if self.run_time_fs is not None else self.pulse_fwhm_fs * 8


@dataclass(frozen=True)
class ModeProfileParams:
    wavelength_um: float = 1.55
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
    """Cladding half-extent (each side of the core) the solver/sim should use.

    Normally the project's chosen clad_thickness_um. When clad_infinite is set,
    a wavelength-scaled extent large enough that the mode no longer interacts
    with the domain boundary — the "assume infinite cladding depth" mode."""
    if settings.clad_infinite:
        return _INFINITE_CLAD_WAVELENGTHS * wavelength_um
    return settings.clad_thickness_um


def _layout_bbox_um(document: LayoutDocument):
    bbox = document.top.bbox()
    if bbox.empty():
        raise ValueError("Cannot run an FDTD simulation on an empty layout — place at least one component first.")
    return bbox


def estimate_grid_cell_count(document: LayoutDocument, params: FdtdParams = FdtdParams()) -> int:
    """Cheap pre-flight check: builds only the Grid (no structures, no
    source, no monitor, no run) to estimate the total cell count a real
    run would use, so a caller can warn before committing to an expensive
    simulation. True 3D now (no z-collapse), so this includes the full
    core+cladding vertical extent."""
    pf = _import_photonfdtd()
    bbox = _layout_bbox_um(document)
    settings = document.project_settings
    cell_size_m = params.resolved_cell_size_um() * 1e-6
    padding_m = params.padding_um * 1e-6
    x_extent = (bbox.right - bbox.left) * 1e-6 + 2 * padding_m
    y_extent = (bbox.top - bbox.bottom) * 1e-6 + 2 * padding_m
    clad_thickness_um = effective_clad_thickness_um(settings, params.wavelength_um)
    z_extent = settings.thickness_um * 1e-6 + 2 * clad_thickness_um * 1e-6 + 2 * padding_m
    grid = pf.Grid(size=(x_extent, y_extent, z_extent), cell_size=cell_size_m, pml_layers=(12, 12, 12))
    return int(grid.shape[0]) * int(grid.shape[1]) * int(grid.shape[2])


def estimate_run_seconds(grid_shape: tuple[int, int, int], n_steps: int) -> float:
    """Rough, empirically-calibrated estimate (see _SECONDS_PER_CELL_STEP's
    docstring) — not a guarantee, just enough to warn before a run that
    would otherwise look like a frozen app."""
    total_cells = int(grid_shape[0]) * int(grid_shape[1]) * int(grid_shape[2])
    return total_cells * int(n_steps) * _SECONDS_PER_CELL_STEP


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
    return pf.ModeSolver(
        size=(ly, lz),
        cell_size=cell_size_m,
        structures=[core_box],
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
        angle = math.radians(spec.direction_deg)
        ux, uy = math.cos(angle), math.sin(angle)
        n_seg = max(spec.cherenkov_segments, 2)
        span_m = spec.cherenkov_length_um * 1e-6
        step_m = span_m / (n_seg - 1)
        fwhm_s = spec.fwhm_fs * 1e-15
        dipoles = []
        for i in range(n_seg):
            s = i * step_m  # arc length from the track start
            pos = (spec.x_um * 1e-6 + ux * s, spec.y_um * 1e-6 + uy * s, 0.0)
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


def build_simulation(document: LayoutDocument, params: FdtdParams = FdtdParams()) -> Any:
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
    bbox = _layout_bbox_um(document)
    settings = document.project_settings

    core = pf.Medium.from_index(settings.core_index, name=settings.platform_name)
    clad_n = params.clad_index if params.clad_index is not None else settings.clad_index
    clad = pf.Medium.from_index(clad_n, name="cladding")
    thickness_m = settings.thickness_um * 1e-6
    clad_thickness_m = effective_clad_thickness_um(settings, params.wavelength_um) * 1e-6
    cell_size_m = params.resolved_cell_size_um() * 1e-6
    padding_m = params.padding_um * 1e-6

    sim = pf.from_gdsfactory(
        document.top,
        layers={1: (core, (0.0, thickness_m))},
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
        use_gpu=params.use_gpu,
        use_numba=params.use_numba,
    )

    sources = params.sources
    if not sources:
        sources = (SourceSpec(x_um=bbox.left, y_um=(bbox.top + bbox.bottom) / 2, wavelength_um=params.wavelength_um, fwhm_fs=params.pulse_fwhm_fs),)
    for spec in sources:
        built = build_source(settings, spec)
        # A cherenkov spec expands into a whole track of point dipoles.
        for src in built if isinstance(built, list) else [built]:
            sim.add_source(src)

    sim.add_monitor(pf.FieldMonitor(name="field", components=("Ez", "Ey"), interval=params.monitor_interval))

    return sim


def run_simulation(sim: Any) -> Any:
    """Trivial wrapper kept separate from build_simulation so a threading
    layer only has to call this one function — the actual compute, with
    no Qt/threading concerns mixed in, stays directly unit-testable."""
    return sim.run()
