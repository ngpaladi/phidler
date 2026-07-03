# FAQ & Troubleshooting

## General

### How is this different from klayout or writing gdsfactory scripts?

Phidler sits on top of gdsfactory and gives it an interactive desktop UI. You
place PDK components by dragging, route with the mouse, and simulate — without
writing and re-running a Python script for every change. Unlike a pure GDS
editor (klayout), the components are live, parametric gdsfactory factories with
real ports, so routing, length matching, and the FDTD simulation understand your
layout, not just its polygons. When you *do* want code, the
[scripting console](guide.md#scripting-console) and the "Export Python Script"
action bridge back to gdsfactory.

### Do I need to know Python?

No. The whole place → route → simulate → export flow is UI-driven. The scripting
console is there when you want it, not a requirement.

### Where are my projects saved?

In an editable `.phidler` file (JSON) that captures the design *and* the
simulation set-up (sources, run parameters). Re-opening restores both.

## Installation & launching

### The `Simulate` button opens a window but a run needs `photonfdtd` — what is that?

`photonfdtd` is the FDTD solver, a separate package that isn't on PyPI. If it's
missing when you click **Simulate**, Phidler offers to download and install it
from GitHub for you — approve the prompt and it fetches it into your
environment. You can also install it manually; see the
[FDTD section of the guide](guide.md#fdtd-simulation).

### On Linux, the app crashes on launch with an `undefined symbol` error

That's a conflict between your system's Qt6 and the newer Qt6 bundled inside
PySide6. Launch with `./run.sh`, which prepends PySide6's own Qt library path so
the right version loads first. (Details in
[Development → environment notes](development.md#environment-notes).)

### I don't have a virtualenv set up yet

Just run `./run.sh` (Linux/macOS). On first launch it creates the `.venv` and
installs Phidler for you if one isn't there. On Windows, create the venv and
`pip install -e ".[dev]"` as shown on the [home page](index.md#installation),
then launch with `python -m phidler`.

## Layout & routing

### My length-matched route didn't hit the goal exactly

With **Auto** on, Phidler inserts an adiabatic meander to approach the goal
length, but it's bounded by the space and bend radius available. Select the
route to see the actual length and the delta from your goal. If it can't get
close, give the arm more room, lower the goal, or route the *other* arm shorter
so the meander has slack to work with.

### A route took a long detour / U-turned unexpectedly

Routing respects port orientation. Turn on **Diagonal** (toolbar) to allow
all-angle bends and take the short path; it falls back to Manhattan when a port
pair can't be routed diagonally, and is ignored when a length goal is set.

### Components snap to positions I didn't intend

**Snap** (toolbar) rounds placement, dragging, and routing to the grid pitch.
Turn it off for free positioning, or change the **Grid (µm)** pitch to match the
resolution you want.

### The coordinate readout shows time (fs/ns) instead of microns

The **Units** dropdown switched to a propagation-time view. Set it back to
**µm (spatial)**. Time units convert distance using the effective index from
your last mode solve (or the core index if you haven't run one).

## DRC

### DRC flags violations in my meander

The meander bends can dip below your **Min spacing** / **Min width** thresholds.
Remember these are *your* entered thresholds, checked geometrically — they are
**not** validated against any official foundry rule deck. Loosen the thresholds,
or give the route more room. Click a violation in the list to zoom straight to
it.

## FDTD simulation

### The mode profile looks cut off at the top or bottom

The cladding in the simulation window is too thin to contain the mode's
evanescent tail, so it's being truncated. Increase the cladding thickness in
[Project Settings](guide.md#project-settings); the mode solver warns when the
window clips the field.

### Is the FDTD result a real, calibrated simulation?

Treat it as a qualitative look at how light spreads through your structure — the
same spirit as the waveguide-width estimate elsewhere in the app. The solver is
a real Yee-grid FDTD, but the mode solver is scalar (no TE/TM distinction) and
the "single photon" framing is semi-classical. It's for intuition and checking
that a device behaves, not a substitute for a calibrated photonic simulator.

### Can I use an AMD GPU, not just NVIDIA?

Yes. GPU acceleration goes through CuPy, and Phidler works with either the CUDA
build (NVIDIA, `pip install cupy-cuda12x`) or the ROCm build (AMD,
`pip install cupy-rocm-5-0`). The status line reports which backend actually ran
(`Done on GPU (CUDA)…` / `…(ROCm)…`).

### The GPU checkbox is greyed out

CuPy isn't installed (or its build doesn't match your driver). Install the
[right CuPy wheel](guide.md#fdtd-simulation) for your GPU. Numba (CPU JIT) is a
lighter alternative and is on by default when installed.

### A run is going to be huge — will it freeze the app?

Before a large run, Phidler shows an estimate of the memory and time it'll take
and asks you to confirm. The solve itself runs off the UI thread (GPU runs in a
separate process), so the window stays responsive, and you can offload to a
[remote server](guide.md#fdtd-simulation) if your machine can't fit it.

## Still stuck?

Open an issue at
[github.com/ngpaladi/phidler](https://github.com/ngpaladi/phidler/issues) with
what you did and what happened.
