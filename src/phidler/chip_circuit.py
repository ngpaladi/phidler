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

import warnings
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

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


def extra_cells() -> dict[str, Callable]:
    """The photonfdtd cells Phidler offers, by registered name."""
    if not AVAILABLE:
        return {}
    return {
        "lnoi_phase_shifter": lnoi.phase_shifter,
        "lnoi_mzi_switch": lnoi.mzi_switch,
        "lnoi_dbeta_coupler_switch": lnoi.dbeta_coupler_switch,
        "waveguide_photodetector": pc_cells.photodetector,
    }


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


def build_circuit(document, config: CircuitConfig) -> ChipCircuit:
    """The circuit of ``document``'s layout. Solves the LN cells'
    cross-sections the first time a design is seen (about a minute each;
    cached on disk after that)."""
    if not AVAILABLE:
        raise RuntimeError(UNAVAILABLE_REASON)
    register_cells()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # dangling-port and off-diagonal Pockels notes
        chip = pc.Chip.from_gdsfactory(
            document.top,
            waveguide=dict(neff=config.neff, ng=config.ng, wl0=config.wavelength,
                           loss_db_cm=config.loss_db_cm),
            bandwidth=(config.bandwidth_ghz * 1e9) or None,
            cell_size=config.cell_size,
        )
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
    return ChipCircuit(chip, labels, members)


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


def operating_point(circ: ChipCircuit, specs, inputs_mw, wavelength: float):
    """The steady state at the drives' t = 0 values: a ChipState."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for v in values:
            states.append(circ.chip.solve(wavelength, drives={**base, net: float(v)},
                                          inputs=_inputs(inputs_mw)))
    return _curves_from_states(circ, states, values, f"{net} (V)")


def wavelength_sweep(circ: ChipCircuit, specs, inputs_mw, wavelengths) -> Curves:
    wl = np.asarray(wavelengths, float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        st = circ.chip.solve(wl, drives=_drives(circ, specs, False), inputs=_inputs(inputs_mw))
    cur = {circ.label(d): st.detector_current[d] * 1e3 for d in circ.detectors}
    pw = {p: st.power[p] * 1e3 for p in circ.optical_ports}
    return Curves(wl, "wavelength (um)", cur, pw)


def time_domain(circ: ChipCircuit, specs, inputs_mw, wavelength: float, t) -> Curves:
    t = np.asarray(t, float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tr = circ.chip.transient(t, _drives(circ, specs, True), inputs=_inputs(inputs_mw),
                                 wavelength=wavelength)
    cur = {circ.label(d): tr.detector_current[d] * 1e3 for d in circ.detectors}
    pw = {p: tr.power[p] * 1e3 for p in circ.optical_ports}
    return Curves(t * 1e9, "time (ns)", cur, pw, {k: v for k, v in tr.drive.items()})
