"""Train a region of the layout like a neural network.

A *design box* on the layout is the trainable part: its etch pattern plays the
role of a network's weights. The rest of the layout stays as drawn. Light enters
through ``N`` chosen input ports and is read at ``M`` output ports. A sample
``x`` is the vector of complex amplitudes driving the inputs, and the model
output is the power detected at each output, ``P_m = |(T x)_m|**2``.

photonfdtd's :mod:`photonfdtd.train` does the heavy lifting. Maxwell's equations
are linear, so for a fixed etch the chip is an exact transfer matrix ``T``
(``M x N``) measured by one simulation per input port, and every sample of a
dataset is then ``T @ x``. A training step therefore costs N forward + N adjoint
simulations, however many samples there are.

Runs use the 2.5-D effective-index reduction (see
:func:`phidler.fdtd_sim.build_eim_reduction`): the design box is a 2-D
in-plane pattern of full-height core and fully-etched cladding, and a 2-D grid
keeps each training step to seconds.

The readouts are not calibrated to absolute power (a point-dipole launch, like
the propagation tab's), so every loss here is scale-invariant:

* ``classify``: cross-entropy on the share of power in each output port (class
  ``m`` is output port ``m``);
* ``regress``: mean-squared error between the output power *fractions* and
  target fractions (each target row sums to one);
* ``transfer``: one minus the fidelity ``|<T, T_target>|**2 / (|T|**2 |T_target|**2)``,
  which ignores the overall scale and global phase of ``T``.

Nothing here imports Qt; the Train tab (``panels.train_tab``) drives it from a
worker thread.
"""

from __future__ import annotations

import csv
import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

from .fdtd_sim import (
    FdtdParams,
    SourceSpec,
    _import_photonfdtd,
    build_eim_reduction,
    build_simulation,
    grid_origin_um,
)
from .model.document import LayoutDocument

_C0 = 299_792_458.0

# The component name a trained design is placed under. Registered with the
# active PDK by register_trained_cell(); the polygons travel in the instance's
# kwargs, so a saved project rebuilds the cell without any sidecar files.
TRAINED_CELL = "trained_design"
_CORE_LAYER = (1, 0)

TASK_KINDS = ("classify", "regress", "transfer")


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Port:
    """A waveguide port in absolute layout coordinates. ``orientation_deg`` is
    the direction the port faces, out of its component (gdsfactory's
    convention): an input stub's free end faces away from the device."""

    name: str
    x_um: float
    y_um: float
    orientation_deg: float
    width_um: float

    @property
    def normal(self) -> tuple[float, float]:
        a = math.radians(self.orientation_deg)
        return (round(math.cos(a), 12), round(math.sin(a), 12))


def _connected(document: LayoutDocument) -> set[tuple[int, str]]:
    used = set()
    for r in document.routes.values():
        used.add((r.instance_id_a, r.port_name_a))
        used.add((r.instance_id_b, r.port_name_b))
    return used


def open_ports(document: LayoutDocument) -> list[Port]:
    """Every component port not joined by a route, in absolute coordinates
    and orientation (the instance's rotation and mirror applied). These are
    the ports light can be launched into or read out of."""
    import klayout.db as kdb

    used = _connected(document)
    ports = []
    for inst_id, inst in document.instances.items():
        t = document.get_transform(inst_id)
        trans = kdb.DCplxTrans(t.mag, t.rotation, t.mirror, t.x, t.y)
        for name, lx, ly, orient, width in document.get_ports_for_instance(inst_id):
            if (inst_id, name) in used:
                continue
            p = trans.trans(kdb.DPoint(lx, ly))
            a = math.radians(orient)
            d = trans.trans(kdb.DVector(math.cos(a), math.sin(a)))
            ang = math.degrees(math.atan2(d.y, d.x)) % 360.0
            ports.append(Port(f"{inst.component_spec}#{inst_id}:{name}", float(p.x), float(p.y),
                              round(ang, 6), float(width) * t.mag))
    return ports


def _axis_of(port: Port) -> str:
    nx, ny = port.normal
    if abs(nx) > 0.99:
        return "x"
    if abs(ny) > 0.99:
        return "y"
    raise ValueError(f"port {port.name} faces {port.orientation_deg:.0f}°; only ports "
                     "facing along x or y can be used for training")


# --------------------------------------------------------------------------- #
# Tasks (datasets)
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    """What to train for. ``X`` is (n, N) complex input amplitudes; ``Y`` is
    (n,) class labels (``classify``) or (n, M) target power fractions
    (``regress``); ``target_T`` is the (M, N) linear map for ``transfer``."""

    kind: str
    name: str = ""
    X: Any = None
    Y: Any = None
    target_T: Any = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TASK_KINDS:
            raise ValueError(f"task kind must be one of {TASK_KINDS}")
        if self.kind == "transfer":
            if self.target_T is None:
                raise ValueError("a transfer task needs target_T")
            self.target_T = np.asarray(self.target_T, complex)
            if self.target_T.ndim != 2 or not np.any(self.target_T):
                raise ValueError("target_T must be a non-zero M x N matrix")
            return
        if self.X is None or self.Y is None:
            raise ValueError(f"a {self.kind} task needs X and Y")
        self.X = np.asarray(self.X, complex)
        if self.X.ndim != 2:
            raise ValueError("X must be (n_samples, n_inputs)")
        if self.kind == "classify":
            self.Y = np.asarray(self.Y).astype(int)
            if self.Y.shape != (self.X.shape[0],):
                raise ValueError("classification labels Y must be (n_samples,)")
        else:
            Y = np.asarray(self.Y, float)
            if Y.ndim != 2 or Y.shape[0] != self.X.shape[0]:
                raise ValueError("regression targets Y must be (n_samples, n_outputs)")
            s = Y.sum(1, keepdims=True)
            if np.any(s <= 0):
                raise ValueError("each regression target row needs some power")
            self.Y = Y / s

    @property
    def n_inputs(self) -> int:
        return int(self.target_T.shape[1] if self.kind == "transfer" else self.X.shape[1])

    @property
    def n_outputs(self) -> int | None:
        if self.kind == "transfer":
            return int(self.target_T.shape[0])
        if self.kind == "regress":
            return int(self.Y.shape[1])
        return int(self.Y.max()) + 1


def priority_encoder_task(n_inputs: int = 4, thermometer: bool = False) -> Task:
    """An ``n_inputs``-line priority encoder, one-hot: each input line is lit
    (amplitude 1) or dark, and the chip must put its light into the output port
    numbered after the *highest* lit line. ``thermometer=True`` keeps only the
    thermometer codes (lines ``0..k`` lit), the patterns a tapped delay line's
    TDC produces; the default trains on every non-empty pattern."""
    if n_inputs < 2:
        raise ValueError("a priority encoder needs at least two inputs")
    if thermometer:
        pats = [[1] * (k + 1) + [0] * (n_inputs - k - 1) for k in range(n_inputs)]
    else:
        pats = [p for p in itertools.product((0, 1), repeat=n_inputs) if any(p)]
    X = np.asarray(pats, complex)
    Y = np.asarray([max(i for i, b in enumerate(p) if b) for p in pats])
    kind = "thermometer codes" if thermometer else f"all {len(pats)} input patterns"
    return Task("classify", f"{n_inputs}-line priority encoder", X, Y,
                description=f"Output port k lights when input k is the highest lit line ({kind}).")


def load_task_file(path: str, kind: str) -> Task:
    """A dataset from ``.npz`` (arrays ``X``, ``Y`` or ``T``) or ``.csv``.

    CSV: a header row, then one sample per row. Columns named ``in0``,
    ``in1``, ... are the input amplitudes (anything Python's ``complex()``
    parses: ``1``, ``0.5+0.5j``, ``-1j``). For ``classify`` a ``label`` column
    holds the class; for ``regress`` columns ``out0``, ``out1``, ... hold the
    target powers. For ``transfer`` each row is one output port and the ``in*``
    columns its matrix row.
    """
    if path.lower().endswith(".npz"):
        with np.load(path) as d:
            if kind == "transfer":
                return Task("transfer", path, target_T=d["T"])
            return Task(kind, path, d["X"], d["Y"])
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path} has no data rows")

    def cols(prefix):
        names = sorted((k for k in rows[0] if k and k.strip().startswith(prefix)
                        and k.strip()[len(prefix):].isdigit()),
                       key=lambda k: int(k.strip()[len(prefix):]))
        if not names:
            raise ValueError(f"{path} has no {prefix}0, {prefix}1, ... columns")
        return names

    ins = cols("in")
    X = np.asarray([[complex(r[k].replace(" ", "")) for k in ins] for r in rows])
    if kind == "transfer":
        return Task("transfer", path, target_T=X)
    if kind == "classify":
        if "label" not in rows[0]:
            raise ValueError(f"{path} needs a 'label' column for classification")
        return Task("classify", path, X, [int(r["label"]) for r in rows])
    outs = cols("out")
    return Task("regress", path, X, [[float(r[k]) for k in outs] for r in rows])


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TrainConfig:
    """The physical set-up of a training run (all lengths µm)."""

    box_um: tuple[float, float, float, float]  # design box (left, bottom, right, top)
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]
    wavelength_um: float = 1.55
    cell_size_um: float | None = None  # None -> wavelength / 20
    run_time_fs: float | None = None  # None -> long enough to cross the device
    pulse_fwhm_fs: float = 8.0
    filter_radius_um: float = 0.1  # minimum feature size of the etch
    margin_um: float = 1.0  # simulated space around the box and the ports
    clad_index: float | None = None

    def resolved_cell_size_um(self) -> float:
        return self.cell_size_um if self.cell_size_um else self.wavelength_um / 20


def _bounds(points, pad):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# Ports' launch/readout planes sit this far inside their waveguide, so the
# point source is inside the core and the readout sees the guided mode.
_PORT_INSET_UM = 0.3


class ChipModel:
    """The trainable chip: N port simulations, the design region and the
    readouts, wrapped in a :class:`photonfdtd.PhotonicModel`."""

    def __init__(self, document: LayoutDocument, config: TrainConfig) -> None:
        import jax

        pf = _import_photonfdtd()
        if not config.inputs or not config.outputs:
            raise ValueError("pick at least one input and one output port")
        names = [p.name for p in config.inputs + config.outputs]
        if len(set(names)) != len(names):
            raise ValueError("a port can't be both an input and an output (or used twice)")
        left, bottom, right, top = config.box_um
        if right <= left or top <= bottom:
            raise ValueError("the design box has no area")
        self.document = document
        self.config = config
        settings = document.project_settings
        dx = config.resolved_cell_size_um()

        pts = [(left, bottom), (right, top)] + [(p.x_um, p.y_um) for p in config.inputs + config.outputs]
        self.region_um = _bounds(pts, config.margin_um)
        self.origin_um = grid_origin_um(document, self.region_um)
        ox, oy = self.origin_um

        reduction = build_eim_reduction(settings, config.wavelength_um, config.clad_index)
        self.reduction = reduction
        if config.run_time_fs:
            run_fs = config.run_time_fs
        else:
            # Long enough for the pulse to cross the whole simulated window
            # twice at the core's group-ish speed, plus the pulse itself.
            l, b, r, t = self.region_um
            span_m = math.hypot(r - l, t - b) * 1e-6
            run_fs = 2.0 * span_m * reduction.n_core / _C0 * 1e15 + 6.0 * config.pulse_fwhm_fs
        self.run_time_fs = run_fs

        params = FdtdParams(
            wavelength_um=config.wavelength_um, cell_size_um=dx, run_time_fs=run_fs,
            pulse_fwhm_fs=config.pulse_fwhm_fs, clad_index=config.clad_index, eim=True,
            padding_um=0.0, precision="float64" if jax.config.jax_enable_x64 else "float32",
        )
        f0 = _C0 / (config.wavelength_um * 1e-6)
        base = build_simulation(document, params, region_um=self.region_um)
        grid = base.grid
        xs = np.asarray(grid.coords[0]) * 1e6 + ox
        ys = np.asarray(grid.coords[1]) * 1e6 + oy
        self.xs_um, self.ys_um = xs, ys

        monitors, readouts = [], []
        for k, p in enumerate(config.outputs):
            ax = _axis_of(p)
            nx, ny = p.normal
            px, py = p.x_um - nx * _PORT_INSET_UM, p.y_um - ny * _PORT_INSET_UM
            name = f"out{k}"
            if ax == "x":
                monitors.append(pf.DFTMonitor(name=name, components=("Ez",), freqs=[f0],
                                              plane_axis="x", plane_position=(px - ox) * 1e-6))
                prof = np.exp(-((ys - py) / (0.6 * p.width_um)) ** 2)
                w = prof[None, :, None]
            else:
                monitors.append(pf.DFTMonitor(name=name, components=("Ez",), freqs=[f0],
                                              plane_axis="y", plane_position=(py - oy) * 1e-6))
                prof = np.exp(-((xs - px) / (0.6 * p.width_um)) ** 2)
                w = prof[None, :, None]
            readouts.append(pf.FieldProjection(name, "Ez", w))

        waveform = pf.GaussianPulse(freq0=f0, fwhm=config.pulse_fwhm_fs * 1e-15)
        self.sims = []
        for p in config.inputs:
            _axis_of(p)
            nx, ny = p.normal
            pos = ((p.x_um - nx * _PORT_INSET_UM - ox) * 1e-6, (p.y_um - ny * _PORT_INSET_UM - oy) * 1e-6, 0.0)
            src = pf.PointDipole(position=pos, component="Ez", waveform=waveform)
            self.sims.append(pf.Simulation(
                grid=grid, structures=base.structures, sources=[src], monitors=list(monitors),
                run_time=base.run_time, courant=base.courant, precision=params.precision,
            ))

        cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
        self.region = pf.DesignRegion(
            center=((cx - ox) * 1e-6, (cy - oy) * 1e-6),
            size=((right - left) * 1e-6, (top - bottom) * 1e-6),
            core_z=(-1.0, 1.0),  # the 2.5-D grid is a single z cell
            eps_solid=reduction.n_core ** 2, eps_void=reduction.n_background ** 2,
            filter_radius=config.filter_radius_um * 1e-6,
        )
        self.model = pf.PhotonicModel(self.sims, self.region, readouts)
        self.shape = self.region.shape(grid)
        bx, by = self.region.coords(grid)
        self.box_xs_um = np.asarray(bx) * 1e6 + ox
        self.box_ys_um = np.asarray(by) * 1e6 + oy

    @property
    def n_inputs(self) -> int:
        return len(self.sims)

    @property
    def n_outputs(self) -> int:
        return self.model.n_outputs

    @property
    def grid_shape(self) -> tuple[int, ...]:
        return tuple(int(s) for s in self.sims[0].grid.shape)

    def rho_phys(self, rho, beta: float):
        return np.asarray(self.region.rho_phys(self.sims[0], rho, beta=beta))

    def transfer(self, rho, beta: float) -> np.ndarray:
        """The (M, N) transfer matrix at the design wavelength."""
        return np.asarray(self.model.transfer(rho, beta=beta))[0]


# --------------------------------------------------------------------------- #
# Losses
# --------------------------------------------------------------------------- #

def _fractions(pred):
    import jax.numpy as jnp
    return pred / (jnp.sum(pred, axis=-1, keepdims=True) + 1e-300)


def _regress_loss(pred, y):
    import jax.numpy as jnp
    return jnp.mean((_fractions(pred) - y) ** 2) * pred.shape[-1]


def _fidelity_loss(target):
    import jax.numpy as jnp
    t = jnp.asarray(target)

    def loss(T):
        T0 = T[0]
        tt = t.astype(T0.dtype)
        num = jnp.abs(jnp.sum(jnp.conj(T0) * tt)) ** 2
        den = jnp.sum(jnp.abs(T0) ** 2) * jnp.sum(jnp.abs(tt) ** 2) + 1e-300
        return 1.0 - num / den
    return loss


def fidelity(T, target) -> float:
    T = np.asarray(T)
    t = np.asarray(target)
    return float(abs(np.vdot(T, t)) ** 2 / (np.vdot(T, T).real * np.vdot(t, t).real))


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #

@dataclass
class TrainOptions:
    steps: int = 60
    learning_rate: float = 0.05
    beta0: float = 2.0
    beta_max: float = 16.0
    init: float = 0.5  # starting density everywhere in the box
    seed: int = 0


@dataclass
class TrainResult:
    rho: np.ndarray  # final latent density (the optimizer's variable)
    rho_phys: np.ndarray  # projected design at the final beta (what gets fabricated)
    beta: float
    T: np.ndarray  # (M, N) transfer matrix of the final design
    history: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    stopped: bool = False
    elapsed_s: float = 0.0


def _schedule(opts: TrainOptions):
    """beta doubles from beta0 to beta_max over the first ~3/4 of the run."""
    pf = _import_photonfdtd()
    n_doublings = max(0, int(round(math.log2(max(opts.beta_max / opts.beta0, 1.0)))))
    every = max(1, int(opts.steps * 0.75) // max(n_doublings, 1)) if n_doublings else opts.steps
    return pf.beta_continuation(opts.beta0, 2.0, every, opts.beta_max)


def evaluate(model: ChipModel, task: Task, T: np.ndarray) -> dict:
    """Task metrics for a transfer matrix: accuracy (classify), RMS fraction
    error (regress) or fidelity (transfer), plus the per-sample predictions."""
    if task.kind == "transfer":
        return {"fidelity": fidelity(T, task.target_T)}
    a = task.X @ T.T  # (n, M)
    P = np.abs(a) ** 2
    frac = P / np.maximum(P.sum(1, keepdims=True), 1e-300)
    if task.kind == "classify":
        acc = float(np.mean(np.argmax(P, 1) == task.Y))
        return {"accuracy": acc, "fractions": frac}
    return {"rms_error": float(np.sqrt(np.mean((frac - task.Y) ** 2))), "fractions": frac}


def train_chip(
    model: ChipModel,
    task: Task,
    options: TrainOptions = TrainOptions(),
    callback: Callable[[dict, np.ndarray], bool | None] | None = None,
) -> TrainResult:
    """Fit the design box to ``task`` by full-batch Adam with projection
    continuation. ``callback(record, rho_phys)`` runs after every step with the
    step's record (``it``, ``value``, ``beta``, task metric) and the current
    projected design; return True from it to stop early."""
    import jax.numpy as jnp

    pf = _import_photonfdtd()
    if task.n_inputs != model.n_inputs:
        raise ValueError(f"the task has {task.n_inputs} inputs but {model.n_inputs} input ports are selected")
    if task.n_outputs is not None and task.n_outputs > model.n_outputs:
        raise ValueError(f"the task needs {task.n_outputs} outputs but only {model.n_outputs} output ports are selected")
    if task.kind == "regress" and task.n_outputs != model.n_outputs:
        raise ValueError(f"the targets have {task.n_outputs} columns but {model.n_outputs} output ports are selected")
    if task.kind == "transfer" and task.target_T.shape != (model.n_outputs, model.n_inputs):
        raise ValueError(f"target matrix is {task.target_T.shape[0]}x{task.target_T.shape[1]}, "
                         f"but {model.n_outputs} outputs x {model.n_inputs} inputs are selected")

    sched = _schedule(options)
    rng = np.random.default_rng(options.seed)
    rho0 = np.clip(options.init + 0.01 * rng.standard_normal(model.shape), 0.0, 1.0)
    # Raw dipole-launch amplitudes are ~1e-14, so detected powers are ~1e-30:
    # in float32 the loss gradient overflows (1/P**2) and turns into NaN.
    # Every loss here is scale-invariant, so divide T by a fixed scale taken
    # from the starting design. That changes nothing but the numerics.
    scale = float(np.abs(model.transfer(jnp.asarray(rho0), sched(0)["beta"])).max()) or 1.0
    if task.kind == "transfer":
        fid = _fidelity_loss(task.target_T)

        def loss_T(T):
            return fid(T / scale)
    else:
        from photonfdtd.train import _cross_entropy

        X = jnp.asarray(task.X)
        Y = jnp.asarray(task.Y)
        lossf = _cross_entropy if task.kind == "classify" else _regress_loss

        def loss_T(T):
            return lossf(model.model.apply(T / scale, X), Y)

    fn = model.model.value_and_grad(lambda T: (loss_T(T), T), has_aux=True)
    last = {}

    def vg(x, **aux):
        (val, T), g = fn(x, aux)
        last["T"] = np.asarray(T)[0]
        return val, g

    stopped = {"flag": False}

    def cb(it, x, rec):
        m = evaluate(model, task, last["T"])
        rec.update({k: v for k, v in m.items() if np.isscalar(v)})
        rec.setdefault("beta", sched(it)["beta"])
        if callback is not None:
            if callback(dict(rec), model.rho_phys(x, rec["beta"])):
                stopped["flag"] = True
                return True
        return False

    t0 = time.time()
    res = pf.minimize(vg, jnp.asarray(rho0), pf.Adam(options.learning_rate),
                      n_iter=options.steps, schedule=sched, callback=cb)
    beta = float(sched(max(len(res.history) - 1, 0))["beta"])
    T = model.transfer(res.x, beta)
    rho = np.asarray(res.x)
    return TrainResult(
        rho=rho, rho_phys=model.rho_phys(rho, beta), beta=beta, T=T,
        history=list(res.history), metrics=evaluate(model, task, T),
        stopped=stopped["flag"], elapsed_s=time.time() - t0,
    )


# --------------------------------------------------------------------------- #
# Placing the result on the layout
# --------------------------------------------------------------------------- #

def design_polygons(model: ChipModel, rho_phys, threshold: float = 0.5) -> list[list[tuple[float, float]]]:
    """The binarized design as closed loops in µm *relative to the box
    centre* (outer boundaries and holes alike, to be combined even-odd)."""
    pf = _import_photonfdtd()
    left, bottom, right, top = model.config.box_um
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    loops = pf.gds.density_polygons(model.box_xs_um, model.box_ys_um, np.asarray(rho_phys), threshold)
    out = []
    for loop in loops:
        # Clip to the box: the contour pad can step half a cell outside.
        lx = np.clip(loop[:, 0], left, right) - cx
        ly = np.clip(loop[:, 1], bottom, top) - cy
        out.append([(round(float(a), 4), round(float(b), 4)) for a, b in zip(lx, ly)])
    return out


def trained_design(polygons=(), layer=_CORE_LAYER, width: float = 1.0, height: float = 1.0):
    """The cell a trained design is placed as: its polygons (µm, centred on
    the cell origin, combined even-odd so holes come out as holes) on the core
    layer. ``width``/``height`` are the design box, kept so the cell's extent
    is the box even where the edge is etched away."""
    import gdsfactory as gf
    import klayout.db as kdb

    c = gf.Component()
    dbu = c.kcl.dbu
    region = kdb.Region()
    for loop in polygons:
        if len(loop) < 3:
            continue
        poly = kdb.Polygon([kdb.Point(round(x / dbu), round(y / dbu)) for x, y in loop])
        region ^= kdb.Region(poly)
    region.merge()
    layer_index = c.kcl.layer(*layer)
    c.shapes(layer_index).insert(region)
    c.info["design_box_um"] = (float(width), float(height))
    return c


def register_trained_cell() -> None:
    """Make ``trained_design`` resolvable by name in the active PDK. Called by
    the app at start-up, by project loading and by the job runner, so a saved
    project holding a trained design reloads anywhere."""
    import gdsfactory as gf

    try:
        pdk = gf.get_active_pdk()
    except Exception:
        return
    if TRAINED_CELL in pdk.cells:
        return
    pdk.register_cells(**{TRAINED_CELL: gf.cell(trained_design)})


def place_trained_design(document: LayoutDocument, model: ChipModel, rho_phys, threshold: float = 0.5):
    """Add the trained design to the layout as a ``trained_design`` instance
    centred on the design box. Returns the PlacedInstance."""
    register_trained_cell()
    left, bottom, right, top = model.config.box_um
    polys = design_polygons(model, rho_phys, threshold)
    kwargs = {
        "polygons": [[list(pt) for pt in loop] for loop in polys],
        "width": round(right - left, 4),
        "height": round(top - bottom, 4),
    }
    return document.add_instance(TRAINED_CELL, kwargs, x=(left + right) / 2.0, y=(bottom + top) / 2.0)


def priority_encoder_stubs(n: int = 4, pitch_um: float = 1.2, gap_um: float = 4.0,
                          stub_um: float = 2.0, width_um: float = 0.5,
                          center_um: tuple[float, float] = (0.0, 0.0)):
    """Where the built-in demo's waveguides go: ``n`` input stubs on the left
    and ``n`` output stubs on the right of an empty gap, line 0 at the bottom.
    Returns ``(placements, box_um)``: ``placements`` is a list of
    ``(role, line, component_spec, kwargs, x, y)`` for add_instance (inputs
    face left out of their ``o1`` port, outputs right out of ``o2``), and
    ``box_um`` is the gap, the design box to train."""
    cx, cy = center_um
    y0 = cy - (n - 1) * pitch_um / 2.0
    kwargs = {"length": stub_um, "width": width_um}
    placements = []
    for k in range(n):
        y = y0 + k * pitch_um
        placements.append(("input", k, "straight", dict(kwargs), cx - gap_um / 2 - stub_um, y))
        placements.append(("output", k, "straight", dict(kwargs), cx + gap_um / 2, y))
    half = n * pitch_um / 2.0
    return placements, (cx - gap_um / 2, cy - half, cx + gap_um / 2, cy + half)


def demo_ports(document: LayoutDocument, placed: list[tuple[str, int, int]]):
    """The (inputs, outputs) Port tuples of demo stubs already placed;
    ``placed`` is ``(role, line, instance_id)`` per stub."""
    ports = {p.name: p for p in open_ports(document)}

    def port(inst_id, name):
        return ports[f"{document.instances[inst_id].component_spec}#{inst_id}:{name}"]

    inputs = tuple(port(i, "o1") for role, _, i in sorted(placed, key=lambda t: t[1]) if role == "input")
    outputs = tuple(port(i, "o2") for role, _, i in sorted(placed, key=lambda t: t[1]) if role == "output")
    return inputs, outputs


def demo_priority_encoder_layout(document: LayoutDocument, n: int = 4, **kwargs):
    """Lay out the built-in demo directly on ``document`` (for scripts and the
    console; the Train tab places the same stubs through undoable commands).
    Returns ``(box_um, inputs, outputs)`` ready for a TrainConfig."""
    placements, box = priority_encoder_stubs(n, **kwargs)
    placed = []
    for role, line, spec, kw, x, y in placements:
        placed.append((role, line, document.add_instance(spec, kw, x=x, y=y).id))
    inputs, outputs = demo_ports(document, placed)
    return box, inputs, outputs
