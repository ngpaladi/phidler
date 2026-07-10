# FAQ & Troubleshooting

## General

### How is this different from klayout or writing gdsfactory scripts?

Phidler sits on top of gdsfactory and gives it an interactive desktop UI. You
place PDK components by dragging, route with the mouse, and simulate, all without
writing and re-running a Python script for every change. Unlike a pure GDS
editor (klayout), the components are live, parametric gdsfactory factories with
real ports, so routing, length matching, and the FDTD simulation understand your
layout, not just its polygons. When you *do* want code, the
[scripting console](guide.md#scripting-console) and the "Export Python Script"
action bridge back to gdsfactory.

### Do I need to know Python?

No. The whole place → route → simulate → export flow is UI-driven. The scripting
console is there if you want it, but it's never required.

### Where are my projects saved?

In an editable `.phidler` file (JSON) that captures both the design and the
simulation set-up (sources, run parameters). Re-opening restores everything.

### Can I just tell it what to build, in plain English?

If you have the [Claude Code](https://claude.com/code) CLI installed and the app
was set up with the `ai` extra (`pip install -e ".[ai]"`), yes. Switch the
Console dock from **Python** to **Ask Claude** and describe what you want.
Claude works through the same live session the console does, so its edits appear
on the canvas and scroll past in the console as it goes, and it reads whatever
you've selected — see [Ask Claude to build it](guide.md#ask-claude-to-build-it).
If the dropdown isn't there, one of those two pieces is missing; the app still
runs exactly as before.

## Installation & launching

### The `Simulate` button opens a window but a run needs `photonfdtd` — what is that?

`photonfdtd` is the FDTD solver, a separate package that isn't on PyPI. If it's
missing when you click **Simulate**, Phidler offers to download and install it
from GitHub for you. Approve the prompt and it fetches it into your
environment. You can also install it manually; see the
[FDTD section of the guide](guide.md#fdtd-simulation).

### On Linux, the app crashes on launch with an `undefined symbol` error

That's a conflict between your system's Qt6 and the newer Qt6 bundled inside
PySide6. Launch with `./run.sh`, which prepends PySide6's own Qt library path so
the right version loads first. (More detail in
[Development → environment notes](development.md#environment-notes).)

### I don't have a virtualenv set up yet

Just run `./run.sh` (Linux/macOS). On first launch, if there isn't one already,
it creates the `.venv` and installs Phidler for you. On Windows, create the venv
and `pip install -e ".[dev]"` as shown on the [home page](index.md#installation),
then launch with `python -m phidler`.

## Layout & routing

### My length-matched route didn't hit the goal exactly

With **Auto** on, Phidler inserts an adiabatic meander to approach the goal
length, but it's limited by the space and bend radius it has to work with. Select
the route to see the actual length and how far off your goal it is. If it can't
get close, give the arm more room, lower the goal, or route the *other* arm
shorter so the meander has some slack.

### A route took a long detour / U-turned unexpectedly

Routing respects port orientation. Turn on **Diagonal** (toolbar) to allow
all-angle bends and take the short path. It falls back to Manhattan when a port
pair can't be routed diagonally, and it's ignored when a length goal is set.

### Components snap to positions I didn't intend

**Snap** (toolbar) rounds placement, dragging, and routing to the grid pitch.
Turn it off for free positioning, or change the **Grid (µm)** pitch to whatever
resolution you want.

### The coordinate readout shows time (fs/ns) instead of microns

The **Units** dropdown switched to a propagation-time view. Set it back to
**µm (spatial)**. Time units convert distance using the effective index from
your last mode solve, or the core index if you haven't run one.

## DRC

### DRC flags violations in my meander

The meander bends can dip below your **Min spacing** / **Min width** thresholds.
Keep in mind these are *your* entered thresholds, checked geometrically. They are
**not** validated against any official foundry rule deck. Loosen the thresholds,
or give the route more room. Click a violation in the list to zoom straight to
it.

## FDTD simulation

### The mode profile looks cut off at the top or bottom

The cladding in the simulation window is too thin to hold the mode's
evanescent tail (the part of the field that leaks outside the core), so it's
being truncated. Increase the cladding thickness in
[Project Settings](guide.md#project-settings); the mode solver warns you when the
window clips the field.

### Is the FDTD result a real, calibrated simulation?

Treat it as a qualitative look at how light spreads through your structure, in
the same spirit as the waveguide-width estimate elsewhere in the app. The solver
is a real Yee-grid FDTD, but the mode solver is scalar (no TE/TM distinction) and
the "single photon" framing is semi-classical. It's for building intuition and
checking that a device behaves, not a substitute for a calibrated photonic
simulator.

### Can I use an AMD GPU, not just NVIDIA?

Yes. As of photonfdtd 0.9 the GPU path is JAX, so the simplest route for either
vendor is a GPU-capable jax (`pip install "jax[cuda12]"` for NVIDIA); tick **JAX**
and it runs on the GPU through XLA. The older **GPU (CuPy)** box still covers AMD
via CuPy's ROCm build (`pip install cupy-rocm-5-0`) or NVIDIA via the CUDA build
(`pip install cupy-cuda12x`), but it's deprecated now, so prefer JAX. Either way
the status line tells you which backend actually ran.

### The GPU checkbox is greyed out

That's the legacy **GPU (CuPy)** box, greyed out because CuPy isn't installed (or
its build doesn't match your driver). You don't need it for GPU work anymore.
Install a GPU jax instead (`pip install "jax[cuda12]"`) and use the **JAX** box.
Numba (a CPU just-in-time compiler) is a lighter alternative, and it's on by
default when installed.

### What are the JAX and Subpixel smoothing options?

**JAX** is photonfdtd's differentiable stepper and, since 0.9, the recommended way
to use the GPU: it runs on the GPU through XLA when one is visible and on the CPU
otherwise, always in the background (slow first compile, fast after). It's greyed
out unless the `jax` package is installed. **Subpixel smoothing** is an accuracy
option: it gives cells that straddle a material edge a blended permittivity so a
slanted or curved boundary isn't forced onto the grid, letting a given cell size
resolve your geometry more faithfully. JAX is exclusive of the GPU/Numba boxes,
and subpixel works everywhere except Numba, so the checkboxes clear each other
where they'd conflict. See [Speed](guide.md#speed) and
[A sharper run](guide.md#a-sharper-run-subpixel-smoothing).

### A run is going to be huge — will it freeze the app?

Before a large run, Phidler shows an estimate of the memory and time it'll take
and asks you to confirm. The solve itself runs off the UI thread (GPU runs in a
separate process), so the window stays responsive. And if your machine can't fit
it, you can offload to a [remote server](guide.md#fdtd-simulation).

## Still stuck?

Open an issue at
[github.com/ngpaladi/phidler](https://github.com/ngpaladi/phidler/issues) and
tell us what you did and what happened.
