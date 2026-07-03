#!/usr/bin/env bash
# Runs the test suite headlessly with the same Qt library fix run.sh uses.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"

# First-run setup: create the project venv and install phidler if it isn't
# there yet, mirroring the documented setup (python3 -m venv .venv;
# pip install -e ".[dev]"). Runs before the QT_LIB_DIR find below, which would
# otherwise fail (set -e) on the missing directory.
if [ ! -x "$PYTHON" ]; then
    echo "No venv at $VENV_DIR — running first-time setup…" >&2
    if ! command -v python3 >/dev/null 2>&1; then
        echo "error: python3 not found on PATH; install Python 3.10+ first." >&2
        exit 1
    fi
    python3 -m venv "$VENV_DIR"
    "$PYTHON" -m pip install --upgrade pip
    ( cd "$SCRIPT_DIR" && "$PYTHON" -m pip install -e ".[dev]" )
fi

QT_LIB_DIR="$(find "$VENV_DIR/lib" -maxdepth 1 -name 'python3.*')/site-packages/PySide6/Qt/lib"

export LD_LIBRARY_PATH="$QT_LIB_DIR:${LD_LIBRARY_PATH:-}"
export QT_QPA_PLATFORM=offscreen

exec "$PYTHON" -m pytest "$SCRIPT_DIR/tests" "$@"
