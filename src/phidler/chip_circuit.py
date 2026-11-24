"""Whole-chip circuit simulation of the layout, on photonfdtd.circuit.

A chip is far too large for FDTD, so the Circuit tab treats the layout as a
circuit: photonfdtd gives every placed component a model (the lithium niobate
switches from their electroded cross-sections, waveguides and routes from
their length, photodetectors from their responsivity), reads the layout's
netlist, follows the metal from each pad to the electrodes it drives, and
solves the optics and the electrical network together.

This module is the GUI-free part: registering the photonfdtd cells with the
active PDK, building the circuit from a :class:`LayoutDocument`, turning the
tab's drive settings into voltages or waveforms, and running the four
analyses (operating point, voltage sweep, wavelength sweep, time domain).
"""
from __future__ import annotations

import contextlib
import warnings
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from phidler.model.document import FOLLOWS_CROSS_SECTION

try:  # photonfdtd >= 0.16
    from photonfdtd import circuit as pc
    from photonfdtd import lnoi
    from photonfdtd.circuit import cells as pc_cells

    AVAILABLE = True
    UNAVAILABLE_REASON = ""
except Exception as exc:  # pragma: no cover - depends on the installed photonfdtd
    pc = lnoi = pc_cells = None
    AVAILABLE = False
    UNAVAILABLE_REASON = (
        "Chip simulation needs photonfdtd with photonfdtd.circuit and photonfdtd.lnoi "
        f"(0.16 or newer): {type(exc).__name__}: {exc}"
    )

#: Palette category of the cells registered here.
CATEGORY = "lithium_niobate"
#: Route cross-section for the lithium niobate ridge.
LN_CROSS_SECTION = "lnoi_rib"


#: The split of the input beamsplitter of :func:`lnoi_mzi_switch`.
MZI_SPLIT = 0.5


def _coupler_cache_file():
    import os
    from pathlib import Path

    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "phidler" / "lnoi_coupler_length.json"


_coupler_lengths: dict[str, float] = {}


def coupler_length_50_50(gap: float, width: float) -> float:
    """Length (µm) of the 50:50 directional coupler of two LN ridges ``width``
    wide and ``gap`` apart (edge to edge) on photonfdtd's default x-cut stack,
    from a mode solve of the coupled ridges. The solve takes a couple of
    seconds, so each design is kept in memory and in
    ``~/.cache/phidler/lnoi_coupler_length.json``. Rounded to the 2 nm drawing
    step."""
    import json

    key = f"gap={float(gap):.4f} width={float(width):.4f} split={MZI_SPLIT} {lnoi.X_CUT!r}"
    path = _coupler_cache_file()
    if key not in _coupler_lengths:
        try:
            _coupler_lengths.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
    if key not in _coupler_lengths:
        length = lnoi.coupler_length(gap=float(gap), width=float(width), stack=lnoi.X_CUT, split=MZI_SPLIT)
        _coupler_lengths[key] = round(round(length / 0.002) * 0.002, 3)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_coupler_lengths, indent=0, sort_keys=True))
        except OSError:
            pass
    return _coupler_lengths[key]


def lnoi_mzi_switch(
    arm_length: float = 5000.0,
    width: float = 1.2,
    coupler_gap: float = 1.0,
    coupler_length: float = 0.0,
    electrode_gap: float = 5.0,
    signal_width: float = 20.0,
    ground_width: float = 60.0,
    bend_length: float = 150.0,
    radius: float = 80.0,
):
    """A 2 x 2 Mach-Zehnder switch on x-cut thin-film lithium niobate that starts
    with a 50:50 beamsplitter.

    Light into ``o1`` or ``o2`` is split 50:50 by a directional coupler, runs
    down two arms of ``arm_length`` between push-pull ground-signal-ground
    electrodes, and is recombined by a second 50:50 coupler onto ``o3``/``o4``.
    ``coupler_length`` 0 (the default) sizes both couplers for a 50:50 split at
    this ``width`` and ``coupler_gap`` (see :func:`coupler_length_50_50`), so
    the split stays 50:50 when the waveguide width changes; any other value is
    drawn as given. ``width`` follows the project waveguide width when the
    project cross-section is ``lnoi_rib``. Otherwise as
    :func:`photonfdtd.lnoi.mzi_switch`."""
    length = float(coupler_length) or coupler_length_50_50(coupler_gap, width)
    return lnoi.mzi_switch(
        arm_length=arm_length, width=width, coupler_gap=coupler_gap, coupler_length=length,
        electrode_gap=electrode_gap, signal_width=signal_width, ground_width=ground_width,
        bend_length=bend_length, radius=radius,
    )


def lnoi_mzi_switch_1x2(
    arm_length: float = 5000.0,
    width: float = 1.2,
    coupler_gap: float = 1.0,
    coupler_length: float = 0.0,
    electrode_gap: float = 5.0,
    signal_width: float = 20.0,
    ground_width: float = 60.0,
    bend_length: float = 150.0,
    input_length: float = 50.0,
    tip_gap: float = 1.0,
    radius: float = 80.0,
):
    """A 1 x 2 Mach-Zehnder switch on x-cut thin-film lithium niobate: one input,
    a 1 x 2 splitter.

    Light into ``o1`` is split 50:50 by a symmetric Y-junction (by symmetry, at
    any width), runs down two arms of ``arm_length`` between push-pull
    ground-signal-ground electrodes, and is recombined by a 50:50 directional
    coupler onto ``o3`` (upper) and ``o4`` (lower). The junction has a blunt
    tip, its branches ``tip_gap`` apart where they part. With equal arms, the
    outputs are 50:50 at 0 V; ``+V_pi/2`` sends all the light to one output and
    ``-V_pi/2`` to the other. ``coupler_length`` 0 (the default) sizes the
    output coupler for 50:50 at this ``width`` and ``coupler_gap``, as in
    :func:`lnoi_mzi_switch`. Otherwise as
    :func:`photonfdtd.lnoi.mzi_switch_1x2`."""
    length = float(coupler_length) or coupler_length_50_50(coupler_gap, width)
    return lnoi.mzi_switch_1x2(
        arm_length=arm_length, width=width, coupler_gap=coupler_gap, coupler_length=length,
        electrode_gap=electrode_gap, signal_width=signal_width, ground_width=ground_width,
        bend_length=bend_length, input_length=input_length, tip_gap=tip_gap, radius=radius,
    )


def extra_cells() -> dict[str, Callable]:
    """The photonfdtd cells Phidler offers, by registered name."""
    if not AVAILABLE:
        return {}
    cells = {
        "lnoi_phase_shifter": lnoi.phase_shifter,
        "lnoi_mzi_switch": lnoi_mzi_switch,
        "lnoi_dbeta_coupler_switch": lnoi.dbeta_coupler_switch,
        "waveguide_photodetector": pc_cells.photodetector,
    }
    if hasattr(lnoi, "mzi_switch_1x2"):  # photonfdtd >= 0.16.2
        cells["lnoi_mzi_switch_1x2"] = lnoi_mzi_switch_1x2
    # The delta-beta switch is left at its own width: its default length is
    # three coupling lengths at 1.2 µm, which another width would break.
    for name in ("lnoi_phase_shifter", "lnoi_mzi_switch", "lnoi_mzi_switch_1x2"):
        if name not in cells:
            continue
        try:
            setattr(cells[name], FOLLOWS_CROSS_SECTION, LN_CROSS_SECTION)
        except (AttributeError, TypeError):  # pragma: no cover - a callable that refuses attributes
            pass
    return cells


# The photonfdtd release that added each of these cells, for telling the user
# why a project that uses one won't open here.
CELL_NEEDS_PHOTONFDTD = {
    "lnoi_phase_shifter": "0.16.0",
    "lnoi_mzi_switch": "0.16.0",
    "lnoi_dbeta_coupler_switch": "0.16.0",
    "waveguide_photodetector": "0.16.0",
    "lnoi_mzi_switch_1x2": "0.16.2",
}


def why_missing(name: str) -> str:
    """A plain explanation of why the component ``name`` isn't available."""
    need = CELL_NEEDS_PHOTONFDTD.get(name)
    if need is None:
        return ("not in the component library; if it is a custom component, "
                "its file may have moved (File > Import Custom Components… loads it again)")
    try:
        import photonfdtd
        have = f"photonfdtd {photonfdtd.__version__} from {photonfdtd.__file__}"
    except ImportError:
        have = "no photonfdtd"
    return (f"needs photonfdtd {need} or newer, and this Phidler has {have}. "
            f"Update photonfdtd (pip install -U photonfdtd), or open the project in "
            f"a Phidler that has it")


def register_cells() -> None:
    """Make the photonfdtd cells and the LN ridge cross-section resolvable by
    name in the active PDK (placeable from the palette, routable, and
    reloadable from a saved project). Called at start-up."""
    if not AVAILABLE:
        return
    import gdsfactory as gf

    try:
        pdk = gf.get_active_pdk()
    except Exception:
        return
    missing = {k: v for k, v in extra_cells().items() if k not in pdk.cells}
    if missing:
        pdk.register_cells(**missing)
    if LN_CROSS_SECTION not in pdk.cross_sections:
        pdk.register_cross_sections(**{LN_CROSS_SECTION: lnoi.rib})


# --------------------------------------------------------------------------- #
# Drives.
# --------------------------------------------------------------------------- #
DRIVE_KINDS = ("float", "dc", "square", "sine", "pulse", "prbs")


@dataclass
class DriveSpec:
    """One net's drive, as the tab edits it.

    ``kind``: ``float`` (undriven), ``dc`` (``low`` volts), ``square``
    (``low``/``high`` at ``freq``), ``sine`` (offset ``low``, amplitude
    ``high``, ``freq``), ``pulse`` (``high`` for ``1/freq`` from t = 0),
    ``prbs`` (PRBS-7 between ``low`` and ``high`` at ``freq`` bits/s).
    """
    kind: str = "float"
    low: float = 0.0
    high: float = 1.0
    freq: float = 1e9

    def waveform(self):
        """The drive for a time-domain run (a number or a function of t), or
        None when undriven."""
        if self.kind == "float":
            return None
        if self.kind == "dc":
            return float(self.low)
        if self.kind == "square":
            return pc.Square(self.low, self.high, freq=self.freq)
        if self.kind == "sine":
            return pc.Sine(self.low, self.high, freq=self.freq)
        if self.kind == "pulse":
            return pc.Pulse(self.low, self.high, start=0.0, width=1.0 / self.freq)
        if self.kind == "prbs":
            return pc.PRBS(self.low, self.high, rate=self.freq)
        raise ValueError(f"unknown drive kind {self.kind!r}")

    def dc_value(self) -> float | None:
        """The value at t = 0, for the steady-state analyses."""
        w = self.waveform()
        if w is None:
            return None
        return float(w) if not callable(w) else float(np.asarray(w(np.array([0.0])))[0])


@dataclass
class CircuitConfig:
    """Circuit settings from the tab. ``wavelength`` in um; waveguides are
    given by their effective and group index and loss; ``bandwidth_ghz`` is
    the LN modulators' electrode bandwidth (0: instantaneous)."""
    wavelength: float = 1.55
    neff: float = 1.89
    ng: float = 2.3
    loss_db_cm: float = 0.3
    bandwidth_ghz: float = 30.0
    cell_size: float = 0.05


@dataclass
class ChipCircuit:
    """A built circuit and how its parts map back to the layout."""
    chip: object
    labels: dict[str, str]                 # netlist name -> "#id spec" for display
    net_members: dict[str, list[str]] = field(default_factory=dict)
    #: photonfdtd's notes from the last analysis (floating electrodes,
    #: detectors with no circuit, non-convergence), for the tab to show.
    notes: list[str] = field(default_factory=list)
    #: The per-component FDTD runs behind the passive parts' models (a
    #: :class:`phidler.component_sim.LayoutSParams`), or None when every
    #: model is photonfdtd's own.
    fdtd: object = None

    @property
    def nets(self) -> list[str]:
        return list(self.chip.net_names)

    @property
    def optical_ports(self) -> list[str]:
        return list(self.chip.network.external)

    @property
    def detectors(self) -> list[str]:
        return [n for n, m in self.chip.models.items() if isinstance(m, pc.Photodetector)]

    def label(self, name: str) -> str:
        return self.labels.get(name, name)

    def model_kind(self, name: str) -> str:
        """How instance ``name`` is modelled, for the tab's component list."""
        kind = type(self.chip.models[name]).__name__
        if self.fdtd is not None and name in self.fdtd.models:
            return f"S-matrix, {self.fdtd.how(name)}"
        return kind


def build_circuit(document, config: CircuitConfig, fdtd=None, progress=None,
                  cancel=None) -> ChipCircuit:
    """The circuit of ``document``'s layout. Solves the LN cells'
    cross-sections the first time a design is seen (about a minute each;
    cached on disk after that).

    ``fdtd`` (a :class:`phidler.component_sim.ComponentSimConfig`) measures
    the passive components' S-matrices by FDTD, one component at a time,
    instead of using photonfdtd's ideal models: see
    :mod:`phidler.component_sim`. With ``fdtd.drive_voltages`` the driven
    components (modulators, heaters) are run too, at those voltages.
    ``progress(text)`` is called as that proceeds and ``cancel()`` is polled
    between runs."""
    if not AVAILABLE:
        raise RuntimeError(UNAVAILABLE_REASON)
    register_cells()
    measured = None

    def chip_with(models):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # dangling-port and off-diagonal Pockels notes
            return pc.Chip.from_gdsfactory(
                document.top,
                models=models,
                waveguide=dict(neff=config.neff, ng=config.ng, wl0=config.wavelength,
                               loss_db_cm=config.loss_db_cm),
                bandwidth=(config.bandwidth_ghz * 1e9) or None,
                cell_size=config.cell_size,
                wavelength=config.wavelength,  # LN cross-sections solved here too
            )

    if fdtd is not None:
        from phidler import component_sim

        # Running the driven parts at a voltage starts from photonfdtd's own
        # models of them (how far each volt moves the index, and where).
        builtin = chip_with(None).models if fdtd.drive_voltages else None
        measured = component_sim.layout_sparams(
            document, fdtd, progress=progress, cancel=cancel, builtin=builtin,
            lnoi_kwargs=dict(cell_size=config.cell_size, wavelength=config.wavelength))
        if progress is not None:
            progress("Joining the components into a circuit…")
    chip = chip_with(dict(measured.models) if measured is not None else None)
    # Label instances by the layout's own ids where we can match them.
    by_ref = {inst.ref.name: inst for inst in document.instances.values()}
    labels = {}
    for name, ref in getattr(chip, "instances", {}).items():
        placed = by_ref.get(getattr(ref, "name", None))
        labels[name] = f"#{placed.id} {placed.component_spec}" if placed else name
    members: dict[str, list[str]] = {n: [] for n in chip.net_names}
    for (inst, cond), k in chip._conductor_net.items():
        m = chip.models[inst]
        if m.ports or len(m.conductors) > 1 or isinstance(m, (pc.Heater, pc.Photodetector)):
            members[chip.net_names[k]].append(f"{labels.get(inst, inst)}.{cond}")
    return ChipCircuit(chip, labels, members, fdtd=measured)


def _drives(circ: ChipCircuit, specs: dict[str, DriveSpec], time: bool):
    out = {}
    for net, spec in specs.items():
        v = spec.waveform() if time else spec.dc_value()
        if v is not None:
            out[net] = v
    return out


def _inputs(inputs_mw: dict[str, float]) -> dict[str, float]:
    return {k: v * 1e-3 for k, v in inputs_mw.items() if v > 0}


@dataclass
class Curves:
    """An analysis result: ``x`` with its label, and named y series in two
    groups (photocurrents in mA, optical powers in mW)."""
    x: np.ndarray
    x_label: str
    currents: dict[str, np.ndarray]
    powers: dict[str, np.ndarray]
    drives: dict[str, np.ndarray] = field(default_factory=dict)


#: Warnings from photonfdtd.circuit that change what the results mean.
_NOTE_PATTERNS = ("no circuit between", "but no drive", "did not converge")


@contextlib.contextmanager
def _noted(circ: ChipCircuit):
    """Hide warnings during an analysis, but keep photonfdtd's notes about
    the circuit on ``circ.notes``."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            yield
        finally:
            notes = [str(w.message) for w in caught
                     if any(pat in str(w.message) for pat in _NOTE_PATTERNS)]
            circ.notes = list(dict.fromkeys(notes))


def operating_point(circ: ChipCircuit, specs, inputs_mw, wavelength: float):
    """The steady state at the drives' t = 0 values: a ChipState."""
    with _noted(circ):
        return circ.chip.solve(wavelength, drives=_drives(circ, specs, False),
                               inputs=_inputs(inputs_mw))


def _curves_from_states(circ, states, x, label) -> Curves:
    cur = {circ.label(d): np.array([s.detector_current[d][0] for s in states]) * 1e3
           for d in circ.detectors}
    pw = {p: np.array([s.power[p][0] for s in states]) * 1e3 for p in circ.optical_ports}
    return Curves(np.asarray(x), label, cur, pw)


def voltage_sweep(circ: ChipCircuit, specs, inputs_mw, wavelength: float, net: str,
                  values) -> Curves:
    """Sweep one net's DC voltage, the others at their t = 0 values."""
    base = _drives(circ, specs, False)
    states = []
    with _noted(circ):
        for v in values:
            states.append(circ.chip.solve(wavelength, drives={**base, net: float(v)},
                                          inputs=_inputs(inputs_mw)))
    return _curves_from_states(circ, states, values, f"{net} (V)")


def wavelength_sweep(circ: ChipCircuit, specs, inputs_mw, wavelengths) -> Curves:
    wl = np.asarray(wavelengths, float)
    with _noted(circ):
        st = circ.chip.solve(wl, drives=_drives(circ, specs, False), inputs=_inputs(inputs_mw))
    cur = {circ.label(d): st.detector_current[d] * 1e3 for d in circ.detectors}
    pw = {p: st.power[p] * 1e3 for p in circ.optical_ports}
    return Curves(wl, "wavelength (um)", cur, pw)


def time_domain(circ: ChipCircuit, specs, inputs_mw, wavelength: float, t) -> Curves:
    t = np.asarray(t, float)
    with _noted(circ):
        tr = circ.chip.transient(t, _drives(circ, specs, True), inputs=_inputs(inputs_mw),
                                 wavelength=wavelength)
    cur = {circ.label(d): tr.detector_current[d] * 1e3 for d in circ.detectors}
    pw = {p: tr.power[p] * 1e3 for p in circ.optical_ports}
    return Curves(t * 1e9, "time (ns)", cur, pw, {k: v for k, v in tr.drive.items()})
