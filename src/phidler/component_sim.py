"""Component-by-component FDTD: one small simulation per part instead of one
large simulation of the whole layout.

An FDTD grid's memory grows with the area it covers, so a whole layout soon
needs more than the machine has. Light in a circuit, though, only passes from
one component to the next through single-mode waveguides, and a component is
then fully described by its S-matrix: how much of the guided mode sent into
one port comes out of each port, and with what phase. This module measures
that S-matrix for each *distinct* component with an FDTD run of that
component alone, and :mod:`phidler.chip_circuit` joins the S-matrices along
the layout's connections. The largest grid ever in memory is the largest
single component, and a component used many times (the bend of every route)
is simulated once.

How one component is measured:

* every optical port is extended with a straight stub of the port's own
  cross-section, running out through the absorbing boundary, so the port
  looks like the waveguide it will be connected to;
* the guided mode of that stub is solved on the simulation's own grid (so it
  is the mode the grid actually carries);
* for each port in turn the mode is launched from the stub into the
  component, and Fourier-transform monitors on every port plane record the
  fields at the requested wavelengths;
* the fields on each plane are split into the mode going in and the mode
  coming out by their overlap with the solved mode. ``S[out, in]`` is the
  outgoing amplitude at ``out`` over the incoming amplitude at ``in``, both
  in units of the square root of the power, so the launch needs no
  calibration.

The run is the 2.5-D effective-index reduction by default (see
:func:`phidler.fdtd_sim.build_eim_reduction`), or full 3-D.

What it leaves out: only the fundamental mode of each port is followed
(light scattered into higher modes or radiation counts as loss), and
components are assumed not to couple to each other except through their
ports, so two parts placed close enough to couple evanescently must be one
component.

Driven components (modulators, heaters) are measured the same way at each
voltage in ``ComponentSimConfig.drive_voltages``: the index change that
photonfdtd's model of the part gives per volt is applied to the waveguide
under its electrodes (:func:`drive_plan`), one set of runs per voltage, and
the circuit gets a modulator or heater whose optics are the measured
S-matrices (:class:`DrivenSParams`). Nothing here imports Qt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from .fdtd_sim import (
    _CORE_LAYER,
    FdtdParams,
    _import_photonfdtd,
    build_simulation,
    estimate_grid_cell_count,
    estimate_memory_gb,
    grid_origin_um,
)

_C0 = 299_792_458.0
_Z0 = 376.730313668
# CPML thickness in cells on x and y; photonfdtd counts it inside the grid.
_N_PML = 12
# Air kept above and below the cladding on a 3-D run (the z axis has no PML,
# so it needs none of the in-plane margin).
_Z_PADDING_UM = 0.5
# Bumped when the measurement changes, so cached S-matrices are not reused.
_CACHE_VERSION = 1

#: Ways a cell can be modelled, for the report.
FDTD, CACHED, ANALYTIC, BUILTIN = "FDTD", "FDTD (cached)", "analytic", "built-in"


class Cancelled(RuntimeError):
    """The caller's ``cancel()`` returned true between two runs."""


@dataclass(frozen=True)
class ComponentSimConfig:
    """Settings of the per-component runs (lengths in um).

    ``span_um`` and ``n_wavelengths`` give the band the S-matrices are
    recorded over, centred on ``wavelength_um``; one pulse covers the whole
    band, so more wavelengths cost no extra runs. ``max_cells`` is the
    largest grid a single component may use: it bounds the memory of the
    whole simulation (about 100 bytes per cell)."""

    wavelength_um: float = 1.55
    span_um: float = 0.1
    n_wavelengths: int = 11
    cell_size_um: float | None = None  # None: wavelength / 30 (2.5-D) or / 20 (3-D)
    eim: bool = True
    eim_polarization: str = "TE"
    clad_index: float | None = None
    pulse_fwhm_fs: float = 25.0
    run_time_fs: float | None = None  # None: from the component's size
    shutoff: float = 1e-4  # stop once the field energy has decayed to this fraction
    margin_um: float | None = None  # None: 1.2 wavelengths around the component
    max_cells: int = 20_000_000
    #: False: simulate every passive component. True: only those photonfdtd
    #: has no model of its own for (bends, tapers and MMIs keep theirs).
    only_unmodelled: bool = False
    #: False: measure every component again (the new results still replace
    #: the cached ones).
    use_cache: bool = True
    #: Sub-cell averaging of the permittivity at material boundaries.
    subpixel: bool = False
    precision: str = "float32"
    #: Voltages to measure the driven components at (modulators: on each
    #: signal electrode, grounds at 0; heaters: across the heater), for
    #: example ``(0.0, 5.0)`` for off and on. Empty: driven components keep
    #: photonfdtd's models and are not simulated.
    drive_voltages: tuple[float, ...] = ()

    def resolved_cell_size_um(self) -> float:
        if self.cell_size_um:
            return float(self.cell_size_um)
        return self.wavelength_um / (30.0 if self.eim else 20.0)

    def resolved_margin_um(self) -> float:
        return float(self.margin_um) if self.margin_um else 1.2 * self.wavelength_um

    def wavelengths_um(self) -> np.ndarray:
        if self.n_wavelengths <= 1 or self.span_um <= 0.0:
            return np.array([float(self.wavelength_um)])
        half = self.span_um / 2.0
        return np.linspace(self.wavelength_um - half, self.wavelength_um + half,
                           int(self.n_wavelengths))

    def padding_um(self) -> float:
        """Grid added around the component: the margin plus the PML."""
        return self.resolved_margin_um() + _N_PML * self.resolved_cell_size_um()


@dataclass(frozen=True)
class CellPort:
    """An optical port in its cell's own frame. ``axis`` is the axis its
    plane is normal to (0: x, 1: y) and ``sign`` the side it faces."""

    name: str
    x_um: float
    y_um: float
    axis: int
    sign: int
    width_um: float


class UnsupportedComponent(ValueError):
    """The component cannot be measured (the message says why)."""


def cell_ports(cell: Any) -> list[CellPort]:
    """The optical ports of ``cell``. Raises :class:`UnsupportedComponent`
    for a port that does not face along x or y."""
    out = []
    for p in cell.ports:
        if p.port_type != "optical":
            continue
        ang = float(p.orientation) % 360.0
        k = round(ang / 90.0)
        if abs(ang - 90.0 * k) > 0.5:
            raise UnsupportedComponent(
                f"port {p.name} faces {ang:.1f} degrees; only ports facing along x or y "
                "can be measured")
        axis, sign = ((0, 1), (1, 1), (0, -1), (1, -1))[k % 4]
        out.append(CellPort(p.name, float(p.center[0]), float(p.center[1]), axis, sign,
                            float(p.width)))
    return out


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def _as_component(cell: Any) -> Any:
    """``cell`` as a gdsfactory Component. An instance's cell comes back
    from the netlist as the kfactory cell underneath, which lacks the
    polygon accessors; this is a view of the same cell, not a copy."""
    if hasattr(cell, "get_polygons"):
        return cell
    import gdsfactory as gf

    return gf.Component(base=cell.base)


def _sim_layers(settings: Any) -> list[tuple[int, int]]:
    """The GDS layers the FDTD build reads: the core and the etch layers."""
    layers = [_CORE_LAYER]
    for etch in getattr(settings, "etch_layers", ()):
        layers.append((int(etch.layer), int(etch.datatype)))
    return layers


def _polygons_um(cell: Any, layers: list[tuple[int, int]]) -> dict[tuple[int, int], list[np.ndarray]]:
    kcl = cell.kcl
    dbu = float(kcl.dbu)
    out: dict[tuple[int, int], list[np.ndarray]] = {}
    for idx, polys in cell.get_polygons().items():
        info = kcl.get_info(idx)
        key = (int(info.layer), int(info.datatype))
        if key not in layers:
            continue
        for p in polys:
            pts = np.array([(pt.x * dbu, pt.y * dbu) for pt in p.each_point_hull()], float)
            if len(pts) >= 3:
                out.setdefault(key, []).append(pts)
    return out


def _facet_intervals(poly: np.ndarray, axis: int, position: float) -> list[tuple[float, float]]:
    """Where the line ``coordinate[axis] == position`` is inside ``poly``:
    intervals of the other coordinate."""
    a = poly[:, axis] - position
    b = np.roll(a, -1)
    t = poly[:, 1 - axis]
    tb = np.roll(t, -1)
    cross = (a > 0) != (b > 0)
    if not cross.any():
        return []
    hits = np.sort(t[cross] + (tb[cross] - t[cross]) * (a[cross] / (a[cross] - b[cross])))
    return [(float(hits[i]), float(hits[i + 1])) for i in range(0, len(hits) - 1, 2)]


def port_stubs(cell: Any, ports: list[CellPort], settings: Any, length_um: float,
               reach_um: float) -> list[tuple[tuple[int, int], list[tuple[float, float]]]]:
    """Rectangles extending every port outward by ``length_um``: whatever
    the cell draws on each simulated layer just inside the port's facet,
    within ``reach_um`` of the port's centre, is continued straight out.
    This carries on a strip, a rib with its slab, or any other
    cross-section without having to know which it is."""
    polys = _polygons_um(cell, _sim_layers(settings))
    inset = 2.0 * float(cell.kcl.dbu)
    stubs = []
    for port in ports:
        centre = (port.x_um, port.y_um)
        plane = centre[port.axis] - port.sign * inset
        far = centre[port.axis] + port.sign * length_um
        tc = centre[1 - port.axis]
        for layer, plist in polys.items():
            for poly in plist:
                for lo, hi in _facet_intervals(poly, port.axis, plane):
                    if hi < tc - reach_um or lo > tc + reach_um:
                        continue
                    lo, hi = max(lo, tc - reach_um), min(hi, tc + reach_um)
                    near = centre[port.axis] - port.sign * inset
                    if port.axis == 0:
                        rect = [(near, lo), (far, lo), (far, hi), (near, hi)]
                    else:
                        rect = [(lo, near), (lo, far), (hi, far), (hi, near)]
                    stubs.append((layer, rect))
    return stubs


def geometry_key(cell: Any, ports: list[CellPort], settings: Any, config: ComponentSimConfig) -> str:
    """A hash of everything the S-matrix depends on: the drawn geometry on
    the simulated layers, the ports, the layer stack and the run settings.
    Two cells with different names but the same shapes share one key."""
    polys = _polygons_um(cell, _sim_layers(settings))
    geo = {
        f"{k[0]}/{k[1]}": sorted(np.round(p, 4).tolist() for p in v) for k, v in sorted(polys.items())
    }
    stack = dict(
        core=round(float(settings.core_index), 6),
        clad=round(float(config.clad_index if config.clad_index is not None else settings.clad_index), 6),
        thickness=round(float(settings.thickness_um), 6),
        clad_thickness=repr(getattr(settings, "clad_thickness_um", None)),
        etch=[(int(e.layer), int(e.datatype), round(float(e.slab_thickness_um), 6))
              for e in getattr(settings, "etch_layers", ())],
    )
    cfg = asdict(config)
    for k in ("max_cells", "only_unmodelled", "use_cache", "drive_voltages"):
        cfg.pop(k)
    blob = json.dumps(dict(v=_CACHE_VERSION, geo=geo, ports=[asdict(p) for p in ports],
                           stack=stack, cfg=cfg), sort_keys=True, default=repr)
    return hashlib.sha1(blob.encode()).hexdigest()


def _region_um(cell: Any, ports: list[CellPort],
               settings: Any = None) -> tuple[float, float, float, float]:
    """The box to simulate: the cell's shapes and its optical ports. With
    ``settings``, only the layers the simulation stamps count, so a driven
    part's electrodes and pads (which the optics do not see) add no grid."""
    xs = [p.x_um for p in ports]
    ys = [p.y_um for p in ports]
    pts = [q for v in _polygons_um(cell, _sim_layers(settings)).values() for q in v] \
        if settings is not None else []
    if pts:
        allp = np.concatenate(pts)
        xs += [float(allp[:, 0].min()), float(allp[:, 0].max())]
        ys += [float(allp[:, 1].min()), float(allp[:, 1].max())]
    else:
        bb = cell.bbox()
        xs += [float(bb.left), float(bb.right)]
        ys += [float(bb.bottom), float(bb.top)]
    return (min(xs), min(ys), max(xs), max(ys))


def _params(config: ComponentSimConfig, run_time_fs: float) -> FdtdParams:
    return FdtdParams(
        wavelength_um=config.wavelength_um, cell_size_um=config.resolved_cell_size_um(),
        run_time_fs=run_time_fs, padding_um=config.padding_um(), padding_z_um=_Z_PADDING_UM,
        pulse_fwhm_fs=config.pulse_fwhm_fs, clad_index=config.clad_index, eim=config.eim,
        eim_polarization=config.eim_polarization, precision=config.precision,
        subpixel=config.subpixel,
    )


def cell_count(cell: Any, ports: list[CellPort], settings: Any, config: ComponentSimConfig,
               optics_only: bool = False) -> int:
    """Grid cells one run of this component uses (no simulation is built)."""
    shim = SimpleNamespace(top=cell, project_settings=settings)
    region = _region_um(cell, ports, settings if optics_only else None)
    return estimate_grid_cell_count(shim, _params(config, 1.0), region)


def _run_time_fs(cell: Any, ports: list[CellPort], settings: Any, config: ComponentSimConfig) -> float:
    if config.run_time_fs:
        return float(config.run_time_fs)
    left, bottom, right, top = _region_um(cell, ports)
    path = math.hypot(right - left, top - bottom)
    try:
        path = max(path, float(cell.info.get("length", 0.0) or 0.0))
    except (TypeError, ValueError, AttributeError):
        pass
    path += 2.0 * config.padding_um()
    # Group index is at most a little above the core index; four crossings
    # leave room for a reflection to come back. This is only the limit: the
    # run stops at ``shutoff``, as soon as the light has left.
    n_group = 1.3 * float(settings.core_index)
    return 4.0 * path * 1e-6 * n_group / _C0 * 1e15 + 8.0 * config.pulse_fwhm_fs


# --------------------------------------------------------------------------- #
# Port modes
# --------------------------------------------------------------------------- #

@dataclass
class _Mode:
    """A port's guided mode in the port's outward frame (o, eta, z), with
    eta = z x o: arrays of shape (n_eta, n_z)."""

    e_eta: np.ndarray
    e_z: np.ndarray
    h_eta: np.ndarray
    h_z: np.ndarray
    n_eff: float

    def fields(self):
        return self.e_eta, self.e_z, self.h_eta, self.h_z


def _overlap(a, b) -> complex | np.ndarray:
    """``sum (E_a* x H_b + E_b x H_a*) . o`` over the plane; ``b`` may carry
    a leading frequency axis."""
    ea_e, ea_z, ha_e, ha_z = a
    eb_e, eb_z, hb_e, hb_z = b
    integrand = (ea_e.conj() * hb_z - ea_z.conj() * hb_e) + (eb_e * ha_z.conj() - eb_z * ha_e.conj())
    return integrand.sum(axis=(-2, -1))


def _slab_mode(eps: np.ndarray, dx_m: float, wavelength_m: float) -> _Mode:
    """Fundamental mode of the 1-D index profile ``eps`` (along eta) for the
    2.5-D grid, whose field is (Ez, H in plane)."""
    n = eps.size
    k0 = 2.0 * math.pi / wavelength_m
    a = np.zeros((n, n))
    i = np.arange(n)
    a[i, i] = -2.0 / dx_m ** 2 + k0 ** 2 * eps
    a[i[:-1], i[:-1] + 1] = a[i[:-1] + 1, i[:-1]] = 1.0 / dx_m ** 2
    vals, vecs = np.linalg.eigh(a)
    beta2 = float(vals[-1])
    n_edge = math.sqrt(max(float(eps[0]), float(eps[-1])))
    if beta2 <= (k0 * n_edge) ** 2:
        raise UnsupportedComponent("no guided mode")
    phi = vecs[:, -1]
    phi = phi * np.sign(phi[np.argmax(np.abs(phi))])
    n_eff = math.sqrt(beta2) / k0
    e_z = phi[:, None].astype(complex)
    zero = np.zeros_like(e_z)
    return _Mode(zero, e_z, -(n_eff / _Z0) * e_z, zero, n_eff)


def _vector_mode(eps: np.ndarray, dx_m: float, wavelength_m: float, polarization: str) -> _Mode:
    """Fundamental mode of the cross-section ``eps`` (n_eta, n_z), from
    photonfdtd's full-vector solver run on that permittivity as gridded."""
    pf = _import_photonfdtd()
    n_eta, n_z = eps.shape
    solver = pf.ModeSolver(size=(n_eta * dx_m, n_z * dx_m), cell_size=dx_m, structures=[],
                           wavelength=wavelength_m, num_modes=1,
                           polarization=polarization.lower())
    solver.eps_r = np.array(eps, float)
    res = solver.solve()
    if len(np.atleast_1d(res.n_eff)) == 0:
        raise UnsupportedComponent("no guided mode")
    n_eff = float(np.atleast_1d(res.n_eff)[0])
    if n_eff ** 2 <= float(max(eps[0].max(), eps[-1].max())):
        raise UnsupportedComponent("no guided mode")
    f = [np.asarray(getattr(res, c)[0], complex) for c in ("Ey", "Ez", "Hy", "Hz")]
    dom = f[0] if np.abs(f[0]).max() >= np.abs(f[1]).max() else f[1]
    phase = np.exp(-1j * np.angle(dom.ravel()[np.argmax(np.abs(dom))]))
    return _Mode(*(c * phase for c in f), n_eff)


def _inward_sign(mode: _Mode) -> float:
    """The outgoing mode of one port is the incoming mode of the port it is
    joined to, seen turned half a turn about z. Turning the mode around flips
    eta and the in-plane vectors, which is the reversed mode (H flipped) up
    to this sign: -1 for a mode whose in-plane E is even in eta (TE-like),
    +1 for one whose Ez is even (TM-like, and the 2.5-D field)."""
    turned = -mode.e_eta[::-1] * mode.e_eta.conj() + mode.e_z[::-1] * mode.e_z.conj()
    return -1.0 if float(np.real(turned.sum())) < 0 else 1.0


# --------------------------------------------------------------------------- #
# One component
# --------------------------------------------------------------------------- #

@dataclass
class ComponentSParams:
    """The measured S-matrix of one component. ``s[(i, o)]`` is the complex
    amplitude leaving port ``o`` per unit amplitude entering port ``i``,
    over ``wavelengths_um``, with the phase convention of
    :mod:`photonfdtd.circuit` (a length L of waveguide is ``exp(-1j beta L)``)."""

    key: str
    ports: tuple[str, ...]
    wavelengths_um: np.ndarray
    s: dict[tuple[str, str], np.ndarray]
    n_eff: dict[str, float] = field(default_factory=dict)
    grid_shape: tuple[int, int, int] = (0, 0, 0)
    seconds: float = 0.0
    #: Ports whose run reached its time limit before the light had left (a
    #: resonant component): their columns are less accurate.
    unconverged: tuple[str, ...] = ()

    @property
    def cells(self) -> int:
        return int(np.prod(self.grid_shape))

    @property
    def memory_gb(self) -> float:
        return estimate_memory_gb(self.cells)

    def power_out(self, port_in: str) -> np.ndarray:
        """Total guided power out of all ports per unit power into
        ``port_in``; what is missing from 1 was radiated or scattered."""
        return sum(np.abs(v) ** 2 for (i, _o), v in self.s.items() if i == port_in)

    def table(self) -> dict:
        """The S-matrix as a photonfdtd ``sparams`` table."""
        return dict(
            wavelength=[float(w) for w in self.wavelengths_um], drives={}, ports=list(self.ports),
            S={f"{i},{o}": {"re": np.real(v).tolist(), "im": np.imag(v).tolist()}
               for (i, o), v in self.s.items()},
        )

    def model(self) -> Any:
        """A :class:`photonfdtd.circuit.Model` interpolating the table."""
        from photonfdtd.circuit.library import TableModel

        return TableModel(self.table(), {}).model

    def to_json(self) -> dict:
        return dict(key=self.key, table=self.table(), n_eff=self.n_eff,
                    grid_shape=list(self.grid_shape), seconds=self.seconds,
                    unconverged=list(self.unconverged))

    @classmethod
    def from_json(cls, d: dict) -> "ComponentSParams":
        t = d["table"]
        s = {}
        for k, v in t["S"].items():
            i, o = k.split(",", 1)
            s[(i, o)] = np.asarray(v["re"], float) + 1j * np.asarray(v["im"], float)
        return cls(d["key"], tuple(t["ports"]), np.asarray(t["wavelength"], float), s,
                   dict(d.get("n_eff", {})), tuple(d.get("grid_shape", (0, 0, 0))),
                   float(d.get("seconds", 0.0)), tuple(d.get("unconverged", ())))


class _PortPlane:
    """Where one port is launched and measured on the grid."""

    def __init__(self, port: CellPort, ports: list[CellPort], grid: Any, origin_um, config,
                 two_d: bool) -> None:
        self.port = port
        dx = config.resolved_cell_size_um()
        t_axis = 1 - port.axis
        self.t_axis = t_axis
        centre = (port.x_um - origin_um[0], port.y_um - origin_um[1])
        self.centre_um = centre
        self._grid_coord = grid.coords[port.axis]
        coords_n = np.asarray(grid.coords[port.axis]) * 1e6
        coords_t = np.asarray(grid.coords[t_axis]) * 1e6
        self.i_plane = int(np.argmin(np.abs(coords_n - centre[port.axis])))
        offset = max(0.5 * config.resolved_margin_um(), 6.0 * dx)
        self.source_um = centre[port.axis] + port.sign * offset
        self.i_source = int(np.argmin(np.abs(coords_n - self.source_um)))
        # The mode window: wide enough for the evanescent tail, but inside the
        # PML and no further than halfway to a neighbouring port on the plane.
        half = 0.5 * port.width_um + 0.8 * config.resolved_margin_um()
        for other in ports:
            if other is port or other.axis != port.axis or other.sign != port.sign:
                continue
            on_plane = abs((other.x_um, other.y_um)[port.axis] - (port.x_um, port.y_um)[port.axis])
            gap = abs((other.x_um, other.y_um)[t_axis] - (port.x_um, port.y_um)[t_axis])
            if on_plane < 2.0 * offset and gap > 0.0:
                half = min(half, 0.5 * gap)
        lo = max(centre[t_axis] - half, coords_t[_N_PML])
        hi = min(centre[t_axis] + half, coords_t[-_N_PML - 1])
        idx = np.flatnonzero((coords_t >= lo) & (coords_t <= hi))
        if idx.size < 5:
            raise UnsupportedComponent(f"port {port.name} is too narrow for the cell size")
        self.t_slice = slice(int(idx[0]), int(idx[-1]) + 1)
        self.t_coords_m = coords_t[self.t_slice] * 1e-6
        # eta = z x o: +y for a port facing +x, -y for -x, -x for +y, +x for -y.
        self.eta_sign = port.sign if port.axis == 0 else -port.sign
        self.two_d = two_d
        self.monitor = f"port_{port.name}"

    def to_local(self, plane: np.ndarray, in_plane: bool) -> np.ndarray:
        """A (…, n_t, n_z) array on the grid, as a function of (eta, z);
        ``in_plane`` for the eta component of a vector."""
        if self.eta_sign < 0:
            plane = plane[..., ::-1, :]
            if in_plane:
                plane = -plane
        return plane

    def eps_local(self, eps: np.ndarray) -> np.ndarray:
        if self.port.axis == 0:
            cut = eps[self.i_source, self.t_slice, :]
        else:
            cut = eps[self.t_slice, self.i_source, :]
        return self.to_local(np.asarray(cut, float), False)

    def sim_fields(self, dft: dict) -> tuple:
        """The monitor's tangential fields in the port frame, (n_f, n_eta, n_z)."""
        t = "y" if self.port.axis == 0 else "x"
        out = []
        for comp, in_plane in ((f"E{t}", True), ("Ez", False), (f"H{t}", True), ("Hz", False)):
            a = np.asarray(dft[comp])
            a = a[:, 0, self.t_slice, :] if self.port.axis == 0 else a[:, self.t_slice, 0, :]
            out.append(self.to_local(a, in_plane))
        return tuple(out)

    def dft_monitor(self, pf: Any, freqs) -> Any:
        t = "y" if self.port.axis == 0 else "x"
        return pf.DFTMonitor(
            name=self.monitor, components=(f"E{t}", "Ez", f"H{t}", "Hz"), freqs=list(freqs),
            plane_axis="xy"[self.port.axis],
            plane_position=float(np.asarray(self._grid_coord)[self.i_plane]),
        )

    def source(self, pf: Any, mode: _Mode, grid: Any, waveform: Any) -> Any:
        """A soft source with the mode's profile, in the stub outside the
        port plane. It radiates both ways: one half goes straight into the
        PML, the other through the port plane into the component."""
        t = "y" if self.port.axis == 0 else "x"
        te = np.abs(mode.e_eta).max() >= np.abs(mode.e_z).max()
        prof = np.real(mode.e_eta if te else mode.e_z)
        prof = self.to_local(prof, te)  # the flip is its own inverse
        z = np.asarray(grid.coords[2])
        centre = [0.0, 0.0, 0.0]
        centre[self.port.axis] = float(np.asarray(grid.coords[self.port.axis])[self.i_source])
        centre[self.t_axis] = float(0.5 * (self.t_coords_m[0] + self.t_coords_m[-1]))
        size = [0.0, 0.0, 0.0]
        size[self.t_axis] = float(self.t_coords_m[-1] - self.t_coords_m[0])
        rel_t = self.t_coords_m - centre[self.t_axis]
        if self.two_d:
            return pf.ModeSource(center=tuple(centre), size=tuple(size), component="Ez",
                                 waveform=waveform, profile=prof[:, 0], profile_coords=(rel_t,))
        size[2] = float(z[-1] - z[0])
        return pf.ModeSource(center=tuple(centre), size=tuple(size),
                             component=f"E{t}" if te else "Ez", waveform=waveform,
                             profile=prof, profile_coords=(rel_t, z))


def simulate_component(
    cell: Any,
    settings: Any,
    config: ComponentSimConfig = ComponentSimConfig(),
    progress: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    index_change: Any = (),
    key: str | None = None,
) -> ComponentSParams:
    """Measure the S-matrix of ``cell`` (a gdsfactory component) between its
    optical ports with one FDTD run per port. ``settings`` is the project's
    :class:`~phidler.model.document.ProjectSettings` (the layer stack).

    ``index_change`` is a list of ``((x0, y0, x1, y1), dn)``: inside each
    rectangle (um, the cell's frame) the guided mode's effective index is
    raised by ``dn``. That is how a driven component is run at a voltage (see
    :func:`drive_plan`); the simulated box then covers the optical layers
    only, not the electrodes. ``key`` names the result in the cache."""
    import gdsfactory as gf

    pf = _import_photonfdtd()
    t_start = time.time()
    cell = _as_component(cell)
    ports = cell_ports(cell)
    if not ports:
        raise UnsupportedComponent("the component has no optical ports")
    index_change = [(tuple(float(v) for v in r), float(dn)) for r, dn in index_change]
    if key is None:
        key = geometry_key(cell, ports, settings, config)
        if index_change:
            key = hashlib.sha1((key + repr(index_change)).encode()).hexdigest()
    region = _region_um(cell, ports, settings if index_change else None)
    pad = config.padding_um()
    dx = config.resolved_cell_size_um()
    wavelengths = config.wavelengths_um()
    freqs = _C0 / (wavelengths * 1e-6)

    # The component with a stub on every port, as a throwaway cell.
    ext = gf.Component()
    try:
        ext.add_ref(cell)
        reach = 0.5 * max(p.width_um for p in ports) + config.resolved_margin_um()
        for layer, rect in port_stubs(cell, ports, settings, pad + 1.0, reach):
            ext.add_polygon(rect, layer=layer)
        shim = SimpleNamespace(top=ext, project_settings=settings)
        params = _params(config, _run_time_fs(cell, ports, settings, config))
        base = build_simulation(shim, params, region_um=region)
    finally:
        try:
            ext.delete()
        except Exception:  # an older kfactory without Component.delete
            pass
    grid = base.grid
    origin = grid_origin_um(shim, region)
    two_d = int(grid.shape[2]) == 1
    eps = np.asarray(base.eps_r)

    planes = []
    for port in ports:
        planes.append(_PortPlane(port, ports, grid, origin, config, two_d))

    # Each port's mode at every wavelength, on the cross-section as gridded.
    modes: list[list[_Mode]] = []
    for plane in planes:
        cut = plane.eps_local(eps)
        try:
            if two_d:
                per_wl = [_slab_mode(cut[:, 0], dx * 1e-6, w * 1e-6) for w in wavelengths]
            else:
                per_wl = [_vector_mode(cut, dx * 1e-6, w * 1e-6, config.eim_polarization)
                          for w in wavelengths]
        except UnsupportedComponent as exc:
            raise UnsupportedComponent(
                f"port {plane.port.name} ({plane.port.width_um:g} um wide) guides no mode at "
                f"{config.wavelength_um:g} um in this layer stack") from exc
        modes.append(per_wl)
    centre_index = int(np.argmin(np.abs(wavelengths - config.wavelength_um)))
    norms = [np.array([abs(_overlap(m.fields(), m.fields())) for m in per_wl]) for per_wl in modes]
    monitors = [plane.dft_monitor(pf, freqs) for plane in planes]
    waveform = pf.GaussianPulse(freq0=_C0 / (config.wavelength_um * 1e-6),
                                fwhm=config.pulse_fwhm_fs * 1e-15)

    # A drive: raise the permittivity by the same amount everywhere the mode
    # reaches inside each rectangle. How far that moves the mode index is
    # read off the port's own cross-section, solved again with the
    # permittivity raised a little (1 / 2n for a plane wave; more for a
    # tightly confined mode, whose field is partly along the guide).
    d_eps = None
    if any(dn for _r, dn in index_change):
        n_ref = modes[0][centre_index].n_eff
        cut, step = planes[0].eps_local(eps), 0.02
        lam = config.wavelength_um * 1e-6
        raised = (_slab_mode(cut[:, 0] + step, dx * 1e-6, lam) if two_d
                  else _vector_mode(cut + step, dx * 1e-6, lam, config.eim_polarization))
        dn_per_eps = (raised.n_eff - n_ref) / step
        if not dn_per_eps > 0.0:
            raise UnsupportedComponent("the port mode does not respond to an index change")
        # On the grid a wave along an axis has (2/dx sin(beta dx/2))^2 where
        # the continuum has beta^2, so the same change moves beta further,
        # by beta dx / sin(beta dx). Take that back out.
        bd = 2.0 * math.pi * n_ref * dx / config.wavelength_um
        per_dn = math.sin(bd) / bd / dn_per_eps
        gx = np.asarray(grid.coords[0]) * 1e6 + origin[0]
        gy = np.asarray(grid.coords[1]) * 1e6 + origin[1]
        d_eps = np.zeros(tuple(int(n) for n in grid.shape))

        def covered(g, lo, hi):
            """The fraction of each cell (centred on ``g``) inside lo..hi."""
            return np.clip((np.minimum(hi, g + dx / 2) - np.maximum(lo, g - dx / 2)) / dx, 0, 1)

        for (x0, y0, x1, y1), dn in index_change:
            weight = np.outer(covered(gx, x0, x1), covered(gy, y0, y1))
            if dn and not weight.any():
                raise UnsupportedComponent("a driven section covers no cell of the grid")
            d_eps += (dn * per_dn * weight)[:, :, None]

    def amplitudes(plane, per_wl, norm, dft):
        """(outgoing, incoming) power amplitudes on ``plane`` per wavelength."""
        sim_f = plane.sim_fields(dft)
        out = np.zeros(len(per_wl), complex)
        inc = np.zeros(len(per_wl), complex)
        for w, m in enumerate(per_wl):
            fw = m.fields()
            bw = (m.e_eta, m.e_z, -m.h_eta, -m.h_z)
            field_w = tuple(c[w] for c in sim_f)
            out[w] = _overlap(fw, field_w) / _overlap(fw, fw) * math.sqrt(norm[w])
            inc[w] = (_inward_sign(m) * _overlap(bw, field_w) / _overlap(bw, bw)
                      * math.sqrt(norm[w]))
        return out, inc

    s: dict[tuple[str, str], np.ndarray] = {}
    unconverged = []
    for k, plane in enumerate(planes):
        if cancel is not None and cancel():
            raise Cancelled("stopped before every component was measured")
        if progress is not None:
            progress(f"port {plane.port.name} ({k + 1} of {len(planes)})")
        src = plane.source(pf, modes[k][centre_index], grid, waveform)
        sim = pf.Simulation(grid=grid, structures=base.structures, sources=[src],
                            monitors=list(monitors), run_time=base.run_time,
                            courant=base.courant, precision=params.precision,
                            subpixel=params.subpixel, subpixel_factor=params.subpixel_factor)
        coefficients = {}
        if d_eps is not None:
            from photonfdtd.jaxbackend import update_coefficients

            ce, ca = update_coefficients(sim, tuple(np.asarray(e) + d_eps
                                                    for e in sim.eps_components()))
            coefficients = dict(ce=ce, ca=ca)
        result = sim.run(disk_tiled=False, shutoff=config.shutoff, **coefficients)
        if len(result.times) >= sim.n_steps:
            unconverged.append(plane.port.name)
        _out_k, inc_k = amplitudes(plane, modes[k], norms[k], result.dft[plane.monitor])
        if np.abs(inc_k).min() <= 0.0:
            raise RuntimeError(f"no light reached port {plane.port.name}'s plane")
        for j, other in enumerate(planes):
            out_j, _inc = amplitudes(other, modes[j], norms[j], result.dft[other.monitor])
            # The monitors accumulate exp(+i w t), so a forward wave reads
            # exp(+i beta x); photonfdtd.circuit uses the conjugate.
            s[(plane.port.name, other.port.name)] = np.conj(out_j / inc_k)
        del sim, result

    # The component is reciprocal (at a fixed drive too): S[a, b] == S[b, a].
    # The two were measured by different runs, so averaging them halves
    # their error.
    for a in range(len(ports)):
        for b in range(a + 1, len(ports)):
            ab, ba = (ports[a].name, ports[b].name), (ports[b].name, ports[a].name)
            s[ab] = s[ba] = 0.5 * (s[ab] + s[ba])

    return ComponentSParams(
        key=key, ports=tuple(p.name for p in ports), wavelengths_um=wavelengths, s=s,
        n_eff={p.name: modes[k][centre_index].n_eff for k, p in enumerate(ports)},
        grid_shape=tuple(int(n) for n in grid.shape), seconds=time.time() - t_start,
        unconverged=tuple(unconverged),
    )


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

_memory_cache: dict[str, ComponentSParams] = {}


def cache_dir() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "phidler" / "component_sparams"


def cache_get(key: str) -> ComponentSParams | None:
    if key in _memory_cache:
        return _memory_cache[key]
    path = cache_dir() / f"{key}.json"
    try:
        sp = ComponentSParams.from_json(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    _memory_cache[key] = sp
    return sp


def cache_put(sp: ComponentSParams) -> None:
    _memory_cache[sp.key] = sp
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        (cache_dir() / f"{sp.key}.json").write_text(json.dumps(sp.to_json()))
    except OSError:
        pass  # a read-only cache directory only costs a re-run next time


def clear_cache() -> None:
    """Forget every stored S-matrix, in memory and on disk."""
    _memory_cache.clear()
    try:
        for f in cache_dir().glob("*.json"):
            f.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Driven components: an S-matrix per drive state
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DrivePatch:
    """A rectangle of the cell (um) whose mode index rises by
    ``sum(slope * value)`` over the drives (volts on a conductor, or watts
    in a heater)."""

    rect: tuple[float, float, float, float]
    slopes: tuple[tuple[str, float], ...]

    def dn(self, values: dict) -> float:
        return float(sum(k * float(values.get(n, 0.0)) for n, k in self.slopes))


@dataclass
class DrivePlan:
    """How one driven component responds: ``axes`` are the drives the
    S-matrix is tabulated over (signal conductors in volts, or ``("P",)``
    for a heater's watts) and ``reference`` the conductors held at 0 V (the
    grounds: the model takes the signals relative to their mean)."""

    kind: str  # "V" or "P"
    axes: tuple[str, ...]
    reference: tuple[str, ...]
    patches: list[DrivePatch]
    resistance: float = 0.0

    def axis_values(self, voltages) -> tuple[float, ...]:
        if self.kind == "P":
            return tuple(sorted({round(float(v) ** 2 / self.resistance, 12) for v in voltages}))
        return tuple(sorted({float(v) for v in voltages}))

    def describe(self, voltages) -> str:
        volts = ", ".join(f"{v:g}" for v in sorted({float(v) for v in voltages}))
        if self.kind == "P":
            return f"{volts} V across the heater"
        return f"{volts} V on {', '.join(self.axes)}"


def _polar_mix(a: np.ndarray, b: np.ndarray, f: float) -> np.ndarray:
    """Between two complex amplitudes: magnitude and phase each linear."""
    mag = (1.0 - f) * np.abs(a) + f * np.abs(b)
    return mag * np.exp(1j * (np.angle(a) + f * np.angle(b * np.conj(a))))


@dataclass
class DrivenSParams:
    """A driven component's S-matrix at each measured drive state.
    ``states[(i, j, ...)]`` is the state at ``values[0][i]`` on the first
    axis of ``plan.axes``, ``values[1][j]`` on the second, and so on."""

    plan: DrivePlan
    values: tuple[tuple[float, ...], ...]
    states: dict[tuple[int, ...], ComponentSParams]
    _fns: dict = field(default_factory=dict, repr=False)

    @property
    def ports(self) -> tuple[str, ...]:
        return next(iter(self.states.values())).ports

    def _state(self, idx: tuple[int, ...], wl: np.ndarray) -> dict:
        if idx not in self._fns:
            self._fns[idx] = self.states[idx].model().fn
        return self._fns[idx](wl)

    def sdict(self, wl, point: dict) -> dict:
        """The S-matrix at drive values ``point`` (by axis name). Exact at
        the measured states; between them magnitude and phase are
        interpolated, and outside them the nearest state is held."""
        wl = np.atleast_1d(np.asarray(wl, float))

        def at(idx: tuple[int, ...]) -> dict:
            k = len(idx)
            if k == len(self.values):
                return self._state(idx, wl)
            axis = np.asarray(self.values[k])
            v = float(np.clip(float(point.get(self.plan.axes[k], 0.0)), axis[0], axis[-1]))
            hi = int(np.searchsorted(axis, v))
            if hi < len(axis) and axis[hi] == v:
                return at(idx + (hi,))
            lo = at(idx + (hi - 1,))
            up = at(idx + (hi,))
            f = (v - axis[hi - 1]) / (axis[hi] - axis[hi - 1])
            return {key: _polar_mix(lo[key], up[key], f) for key in lo}

        return at(())

    def model(self, builtin: Any) -> Any:
        """``builtin`` (photonfdtd's model of this instance: its conductors,
        bandwidth, resistance) with the measured optics in place of its own."""
        from dataclasses import replace

        if set(builtin.ports) != set(self.ports):
            raise UnsupportedComponent(
                f"its model has ports {sorted(builtin.ports)} but the layout has {sorted(self.ports)}")
        axes, reference = self.plan.axes, self.plan.reference
        if self.plan.kind == "P":
            def fn(wl, P=0.0, **_):
                return self.sdict(wl, {"P": float(P)})
        else:
            def fn(wl, V=None, **_):
                V = V or {}
                ref = float(np.mean([float(V.get(r, 0.0)) for r in reference])) if reference else 0.0
                return self.sdict(wl, {n: float(V.get(n, 0.0)) - ref for n in axes})
        return replace(builtin, fn=fn)


def _phase_slope(model: Any, pair: tuple[str, str], wavelength_um: float, settings_of) -> float:
    """d(phase)/d(drive) of ``model``'s transmission ``pair`` about zero
    drive, in rad per unit; ``settings_of(x)`` gives the model's keyword
    arguments at drive ``x``. The models are linear in the drive, so a small
    step (too small to wrap the phase) gives the slope."""
    t0 = complex(np.ravel(model.sdict(wavelength_um, **settings_of(0.0))[pair])[0])
    step = 1e-6
    while step < 1e4:
        t1 = complex(np.ravel(model.sdict(wavelength_um, **settings_of(step))[pair])[0])
        phase = -float(np.angle(t1 / t0))
        if abs(phase) > 1e-7:
            return phase / step
        step *= 10.0
    return 0.0


def drive_plan(cell: Any, info: dict, model: Any, config: ComponentSimConfig,
               lnoi_kwargs: dict | None = None) -> DrivePlan:
    """Where and by how much the drives of ``cell`` change the mode index,
    from photonfdtd's own model of it (``model``):

    - photonfdtd's LN switches (``info["lnoi"]``): each waveguide under each
      electrode section, with the index change per volt that
      :func:`photonfdtd.lnoi.model` solved on the cross-section;
    - any other modulator or heater with two optical ports in line: the
      waveguide between them, with the index change that gives the model's
      phase per volt (or per watt) over that length.

    Anything else raises :class:`UnsupportedComponent`."""
    import photonfdtd.circuit as pc

    ports = cell_ports(cell)
    reach = config.resolved_margin_um()
    rec = info.get("lnoi") if isinstance(info, dict) else None
    if rec is not None and dict(rec).get("kind") != "phase_shifter":
        from photonfdtd import lnoi

        kw = {k: v for k, v in (lnoi_kwargs or {}).items()
              if k in ("stack", "cell_size", "wavelength", "cache") and v is not None}
        m = lnoi.model(cell, **kw)
        slopes = np.asarray(m.slopes, float)
        ribs = [float(y) for y in m.rec["ribs"]]
        patches = []
        for seg in m.rec["segments"]:
            for r, y in enumerate(ribs):
                # Out to the mode's tail, or halfway to the next waveguide.
                half = min([0.5 * float(m.rec["width"]) + reach]
                           + [0.5 * abs(y - o) for i, o in enumerate(ribs) if i != r])
                patches.append(DrivePatch(
                    (float(seg["x0"]), y - half, float(seg["x1"]), y + half),
                    tuple((n, float(slopes[r, strip])) for n, strip in seg["electrodes"].items())))
        signals = tuple(m.signals)
        return DrivePlan("V", signals,
                         tuple(n for n in m.electrode_names if n not in signals), patches)

    if len(ports) != 2:
        raise UnsupportedComponent(
            "only photonfdtd's LN switches and driven parts with two optical ports "
            "can be simulated at a voltage")
    a, b = ports
    t_axis = 1 - a.axis
    pa, pb = (a.x_um, a.y_um), (b.x_um, b.y_um)
    if a.axis != b.axis or a.sign == b.sign or abs(pa[t_axis] - pb[t_axis]) > 1e-3:
        raise UnsupportedComponent("its two optical ports are not in line")
    length = abs(pa[a.axis] - pb[a.axis])
    if length <= 0.0:
        raise UnsupportedComponent("its optical ports coincide")
    half = 0.5 * max(a.width_um, b.width_um) + reach
    lo, hi = sorted((pa[a.axis], pb[a.axis]))
    rect = (lo, pa[1] - half, hi, pa[1] + half) if a.axis == 0 else \
        (pa[0] - half, lo, pa[0] + half, hi)
    # phase = 2 pi dn L / wavelength
    to_dn = config.wavelength_um / (2.0 * math.pi * length)
    pair = (a.name, b.name)
    if isinstance(model, pc.Heater):
        k = _phase_slope(model, pair, config.wavelength_um, lambda x: dict(P=x)) * to_dn
        return DrivePlan("P", ("P",), (), [DrivePatch(rect, (("P", k),))],
                         resistance=float(model.resistance))
    if isinstance(model, pc.Modulator) and model.conductors:
        names = list(model.conductors)
        slopes = tuple(
            (n, _phase_slope(model, pair, config.wavelength_um, lambda x, n=n: dict(V={n: x})) * to_dn)
            for n in names)
        signals = [n for n in names if not n.startswith("gnd")]
        if not signals or len(signals) == len(names):
            signals = names[:1]
        return DrivePlan("V", tuple(signals), tuple(n for n in names if n not in signals),
                         [DrivePatch(rect, slopes)])
    raise UnsupportedComponent("its model takes no voltage")


def _measure_driven(row: "ComponentRow", cell: Any, key: str, plan: DrivePlan, settings: Any,
                    config: ComponentSimConfig, label: str, progress, cancel) -> DrivenSParams:
    """Every drive state of one driven component, from the cache or FDTD."""
    import itertools

    values = tuple(plan.axis_values(config.drive_voltages) for _ in plan.axes)
    unit = "W" if plan.kind == "P" else "V"
    states: dict[tuple[int, ...], ComponentSParams] = {}
    combos = list(itertools.product(*(range(len(v)) for v in values)))
    all_cached = True
    for n, idx in enumerate(combos):
        if cancel is not None and cancel():
            raise Cancelled("stopped before every component was measured")
        point = {a: values[k][i] for k, (a, i) in enumerate(zip(plan.axes, idx))}
        change = [(p.rect, round(p.dn(point), 12)) for p in plan.patches]
        state_key = hashlib.sha1((key + repr(change)).encode()).hexdigest()
        sp = cache_get(state_key) if config.use_cache else None
        if sp is None:
            all_cached = False
            at = ", ".join(f"{a} = {v:.4g} {unit}" for a, v in point.items())
            text = f"{label}, state {n + 1} of {len(combos)} ({at})"
            if progress is not None:
                progress(text)
            sp = simulate_component(
                cell, settings, config, index_change=change, key=state_key,
                progress=(lambda m, text=text: progress(f"{text}: {m}")) if progress else None,
                cancel=cancel)
            cache_put(sp)
        states[idx] = sp
    row.how = CACHED if all_cached else FDTD
    row.sparams = states[combos[0]]
    row.note = (f"driven part, measured at {plan.describe(config.drive_voltages)}; other "
                "drives are interpolated between these and held outside them")
    if any(sp.unconverged for sp in states.values()):
        row.note += ("; light was still inside when a run ended, so set a longer run time "
                     "for a more accurate result")
    return DrivenSParams(plan, values, states)


# --------------------------------------------------------------------------- #
# A whole layout
# --------------------------------------------------------------------------- #

@dataclass
class ComponentRow:
    """One distinct component of the layout, for the report."""

    cell_name: str
    instances: list[str]
    how: str  # FDTD, CACHED, ANALYTIC or BUILTIN
    cells: int = 0
    note: str = ""
    sparams: ComponentSParams | None = None

    @property
    def memory_gb(self) -> float:
        return estimate_memory_gb(self.cells)


@dataclass
class LayoutSParams:
    """Every component's model for :meth:`photonfdtd.circuit.Chip.from_gdsfactory`
    (``models``, by instance name), and what was done for each."""

    models: dict[str, Any] = field(default_factory=dict)
    rows: list[ComponentRow] = field(default_factory=list)
    whole_layout_cells: int = 0

    @property
    def peak_cells(self) -> int:
        return max((r.cells for r in self.rows if r.how in (FDTD, CACHED)), default=0)

    def how(self, instance: str) -> str:
        for r in self.rows:
            if instance in r.instances:
                return r.how
        return BUILTIN

    def summary(self) -> str:
        n = sum(1 for r in self.rows if r.how in (FDTD, CACHED))
        if not n:
            return "No component was simulated by FDTD."
        text = (f"{n} distinct component{'s' * (n != 1)} by FDTD; the largest grid is "
                f"{self.peak_cells:,} cells (about {estimate_memory_gb(self.peak_cells):.2g} GB)")
        if self.whole_layout_cells:
            text += (f", against {self.whole_layout_cells:,} cells "
                     f"(about {estimate_memory_gb(self.whole_layout_cells):.2g} GB) "
                     "for the whole layout in one run")
        return text + "."

    def notes(self) -> list[str]:
        return [f"{r.cell_name}: {r.note}" for r in self.rows if r.note]


#: Cell functions whose length alone describes them exactly.
_STRAIGHTS = ("straight", "straight_all_angle")
#: Cell functions photonfdtd.circuit already has a model for.
_HAS_BUILTIN = ("bend_euler", "bend_circular", "bend_s", "taper", "bend_euler_s",
                "bend_euler_all_angle", "mmi1x2", "mmi2x2")


def _netlist_cells(component: Any) -> tuple[dict, dict[str, Any]]:
    """The netlist of ``component`` and each instance's cell, with the same
    instance names :meth:`photonfdtd.circuit.Chip.from_gdsfactory` uses."""
    from gdsfactory.get_netlist import SmartNamer, function_namer

    inner = SmartNamer(function_namer)
    cells: dict[str, Any] = {}

    def namer(inst):
        name = inner(inst)
        cells[name] = _as_component(inst.cell)
        return name

    net = component.get_netlist(instance_namer=namer, on_dangling_port="ignore")
    return net, cells


def _fdtd_candidate(fname: str, info: dict, cell: Any, config: ComponentSimConfig) -> bool:
    """Whether this cell is a passive optical part to measure by FDTD."""
    port_types = [p.port_type for p in cell.ports]
    if "optical" not in port_types or any(t != "optical" for t in port_types):
        return False  # metal, or a driven part: photonfdtd models those
    if any(k in info for k in ("pcomp", "lnoi", "photodetector")):
        return False
    if fname in _STRAIGHTS:
        return False
    if config.only_unmodelled:
        two_port_length = len(port_types) == 2 and "length" in info
        if fname in _HAS_BUILTIN or two_port_length:
            return False
    return True


def _whole_layout_cells(document: Any, config: ComponentSimConfig) -> int:
    try:
        return estimate_grid_cell_count(document, _params(config, 1.0))
    except Exception:
        return 0


#: Most drive states (voltage combinations) one driven component is run at.
_MAX_DRIVE_STATES = 9


def _driven_plan(name: str, fname: str, info: dict, cell: Any, settings: Any,
                 config: ComponentSimConfig, builtin: Any, lnoi_kwargs: dict | None):
    """``(plan, ports, cells)`` for a driven instance to run at
    ``config.drive_voltages``, ``None`` when it is not one (metal, a
    detector, a component file with its own model), or
    :class:`UnsupportedComponent` saying why it cannot be."""
    import photonfdtd.circuit as pc

    model = (builtin or {}).get(name)
    if (not config.drive_voltages or "pcomp" in info
            or not isinstance(model, (pc.Modulator, pc.Heater)) or not model.ports):
        return None
    ports = cell_ports(cell)
    plan = drive_plan(cell, info, model, config, lnoi_kwargs)
    n_states = len(plan.axis_values(config.drive_voltages)) ** len(plan.axes)
    if n_states > _MAX_DRIVE_STATES:
        raise UnsupportedComponent(
            f"{len(plan.axes)} signal electrodes at {len(set(config.drive_voltages))} voltages "
            f"each is {n_states} states, over the limit of {_MAX_DRIVE_STATES}; give fewer voltages")
    n_cells = cell_count(cell, ports, settings, config, optics_only=True)
    if n_cells > config.max_cells:
        raise UnsupportedComponent(
            f"its grid would be {n_cells:,} cells, over the limit of "
            f"{config.max_cells:,} (about {estimate_memory_gb(n_cells):.2g} GB); use a "
            "coarser cell size or raise the limit")
    return plan, ports, n_cells


def layout_sparams(
    document: Any,
    config: ComponentSimConfig = ComponentSimConfig(),
    progress: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    builtin: dict | None = None,
    lnoi_kwargs: dict | None = None,
) -> LayoutSParams:
    """Measure every distinct passive component of ``document``'s layout,
    one at a time. Straights keep their analytic model and metal and
    detectors keep photonfdtd's. A component too large for
    ``config.max_cells``, or with a port that cannot be measured, falls back
    to photonfdtd's model when it has one (with a note); otherwise
    :class:`UnsupportedComponent` names it.

    Driven parts (modulators, heaters) keep photonfdtd's models too, unless
    ``config.drive_voltages`` lists the voltages to run them at. That needs
    ``builtin``: photonfdtd's model of every instance by name (a plain
    :class:`photonfdtd.circuit.Chip`'s ``models``), which says how each part
    responds to its drive (see :func:`drive_plan`); ``lnoi_kwargs`` are the
    ``cell_size`` and ``wavelength`` its LN cross-sections were solved with."""
    settings = document.project_settings
    net, cells = _netlist_cells(document.top)
    out = LayoutSParams(whole_layout_cells=_whole_layout_cells(document, config))
    by_key: dict[str, ComponentRow] = {}
    todo: list[tuple[ComponentRow, Any, str]] = []

    for name, inst in net["instances"].items():
        cell = cells[name]
        fname = inst["component"]
        info = dict(inst.get("info", {}) or {})
        if not _fdtd_candidate(fname, info, cell, config):
            try:
                driven = _driven_plan(name, fname, info, cell, settings, config, builtin,
                                      lnoi_kwargs)
            except UnsupportedComponent as exc:
                row = by_key.setdefault(f"skip:{fname}:{exc}", ComponentRow(
                    fname, [], BUILTIN, note=f"not simulated ({exc}); photonfdtd's model is used"))
                row.instances.append(name)
                continue
            if driven is not None:
                plan, ports, n_cells = driven
                key = hashlib.sha1((geometry_key(cell, ports, settings, config)
                                    + repr(plan.patches)).encode()).hexdigest()
                if key not in by_key:
                    by_key[key] = ComponentRow(fname, [], FDTD, cells=n_cells)
                    todo.append((by_key[key], cell, key, plan))
                by_key[key].instances.append(name)
                continue
            optical = any(p.port_type == "optical" for p in cell.ports)
            how = ANALYTIC if (fname in _STRAIGHTS or (optical and fname in _HAS_BUILTIN)) else BUILTIN
            row = by_key.setdefault(f"{how}:{fname}", ComponentRow(fname, [], how))
            row.instances.append(name)
            continue
        fallback = fname in _HAS_BUILTIN or (len(list(cell.ports)) == 2 and "length" in info)
        try:
            ports = cell_ports(cell)
            n_cells = cell_count(cell, ports, settings, config)
            if n_cells > config.max_cells:
                raise UnsupportedComponent(
                    f"its grid would be {n_cells:,} cells, over the limit of "
                    f"{config.max_cells:,} (about {estimate_memory_gb(n_cells):.2g} GB); use a "
                    "coarser cell size or raise the limit")
        except UnsupportedComponent as exc:
            if not fallback:
                raise UnsupportedComponent(f"{fname} ({name}): {exc}") from exc
            row = by_key.setdefault(f"skip:{fname}:{exc}", ComponentRow(
                fname, [], ANALYTIC, note=f"not simulated ({exc}); photonfdtd's model is used"))
            row.instances.append(name)
            continue
        key = geometry_key(cell, ports, settings, config)
        if key not in by_key:
            by_key[key] = ComponentRow(fname, [], FDTD, cells=n_cells)
            todo.append((by_key[key], cell, key, None))
        by_key[key].instances.append(name)

    out.rows = list(by_key.values())
    for n, (row, cell, key, plan) in enumerate(todo):
        if cancel is not None and cancel():
            raise Cancelled("stopped before every component was measured")
        if plan is not None:
            label = f"FDTD of {row.cell_name} ({n + 1} of {len(todo)}, {row.cells:,} cells)"
            try:
                measured = _measure_driven(row, cell, key, plan, settings, config, label,
                                           progress, cancel)
                for name in row.instances:
                    out.models[name] = measured.model(builtin[name])
            except UnsupportedComponent as exc:
                raise UnsupportedComponent(f"{row.cell_name} ({row.instances[0]}): {exc}") from exc
            continue
        sp = cache_get(key) if config.use_cache else None
        if sp is not None:
            row.how = CACHED
        else:
            label = f"FDTD of {row.cell_name} ({n + 1} of {len(todo)}, {row.cells:,} cells)"
            if progress is not None:
                progress(label)
            try:
                sp = simulate_component(
                    cell, settings, config,
                    progress=(lambda m, label=label: progress(f"{label}: {m}")) if progress else None,
                    cancel=cancel)
            except UnsupportedComponent as exc:
                raise UnsupportedComponent(f"{row.cell_name} ({row.instances[0]}): {exc}") from exc
            cache_put(sp)
        row.sparams = sp
        if sp.unconverged:
            row.note = ("light was still inside when the run ended (a resonant part?); "
                        "set a longer run time for a more accurate result")
        model = sp.model()
        for name in row.instances:
            out.models[name] = model
    return out
