# Phidler

**Design photonic integrated circuits visually — place, route, simulate, and
export GDS, without writing a line of Python.**

Phidler is a desktop CAD application for photonic integrated circuit (PIC)
layout, built on [gdsfactory](https://gdsfactory.github.io/gdsfactory/). It
puts a real interactive canvas in front of gdsfactory's component library: drag
components from a searchable palette, route between ports with the mouse,
length-match delay lines automatically, check basic design rules, watch light
propagate through your layout with a built-in FDTD engine, and export a
foundry-ready GDS — all in one window.

![Phidler main window](screenshots/main_overview.png)

## Why Phidler?

- **Visual, not scripted.** gdsfactory is powerful but Python-first. Phidler
  gives you the same PDK components on a click-and-drag canvas, so laying out a
  circuit doesn't mean writing and re-running a script.
- **Routing that does the math.** Draw a waveguide between two ports; set a
  target length and Phidler inserts an adiabatic meander to hit it — the delay
  matching an interferometer or a true-time-delay line needs, done for you.
- **See the light.** A built-in [FDTD simulator](guide.md#fdtd-simulation) shows
  the guided mode profile and animates a pulse propagating through your actual
  layout — locally, on a GPU (NVIDIA **or** AMD), or offloaded to a remote box.
- **One window, whole flow.** Palette → canvas → routing → DRC → simulation →
  GDS export, plus a live scripting console when you *do* want to drop into
  gdsfactory. No round-trips between tools.
- **It's still gdsfactory underneath.** Every component is a real gdsfactory
  factory; the export is a real GDSII your foundry/PDK toolchain already
  understands.

New here? Take the **[Feature Tour](tour.md)** to see what it looks like, then
work through the **[MZI Tutorial](tutorial.md)** to build a real circuit
end-to-end in about fifteen minutes.

## Installation

Requires Python 3.10+.

### Linux

```sh
git clone https://github.com/ngpaladi/phidler.git
cd phidler
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### macOS

Requires Python 3.10+ from [python.org](https://www.python.org/downloads/)
or Homebrew (`brew install python@3.12`).

```sh
git clone https://github.com/ngpaladi/phidler.git
cd phidler
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Windows

Requires Python 3.10+ from [python.org](https://www.python.org/downloads/).
Run in PowerShell or Command Prompt:

```
git clone https://github.com/ngpaladi/phidler.git
cd phidler
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Add `docs` to the extras (`pip install -e ".[dev,docs]"`) to also build
this documentation site.

## Running it

**Linux / macOS:**

```sh
./run.sh
```

On first run, `run.sh` creates the `.venv` and installs Phidler for you if it
isn't there yet — so the manual venv steps above are optional if you launch
this way.

**Windows** — `run.sh` is bash-only; launch directly instead:

```
python -m phidler
```

Both show the Project Settings dialog first, where you pick a material
platform before placing components (see
[Project Settings](guide.md#project-settings)).

On Linux, if your system Qt6 conflicts with PySide6's bundled Qt6 (an
`undefined symbol` crash on import), `run.sh` already handles this — see
[Development: environment notes](development.md#environment-notes) for
details.

## Next steps

- [Feature Tour](tour.md) — a screenshot walkthrough of everything Phidler can
  do, from placement to FDTD.
- [MZI Tutorial](tutorial.md) — build a Mach–Zehnder interferometer step by
  step, with routing, length matching, mode solving, DRC, and export.
- [User Guide](guide.md) — the complete reference: place, edit, route, layers,
  DRC, save/export, scripting, and FDTD.
- [Keyboard Shortcuts](shortcuts.md) — the full key and menu reference.
- [FAQ & Troubleshooting](faq.md) — answers to common questions and fixes for
  common snags.
- [Development](development.md) — code layout, test suite, and what's
  verified vs. what still needs manual checking.
