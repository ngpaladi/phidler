# Phidler

A graphical CAD application for photonic integrated circuit (PIC) layout,
built on [gdsfactory](https://gdsfactory.github.io/gdsfactory/) with a
desktop UI, and exportable GDS.

![Phidler main window](screenshots/main_overview.png)

## Installation

```
git clone <this repo>
cd phidler
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running it

```
./run.sh
```

This launches the app and shows the Project Settings dialog first, where
you pick a material platform before you start placing components (see
[Project Settings](guide.md#project-settings)).

If you're on a machine where the system Qt6 conflicts with PySide6's
bundled Qt6 (an `undefined symbol` crash on import), see
[Development: environment notes](development.md#environment-notes) for the
fix `run.sh` already applies.

## Next steps

- [User Guide](guide.md) — how to place, edit, route, save, and export a
  design.
- [Development](development.md) — code layout, test suite, and what's
  verified vs. what still needs manual checking.
