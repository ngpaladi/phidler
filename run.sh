#!/usr/bin/env bash
# Launcher for Phidler. Activates the project venv and works around a Qt
# library conflict: on some Linux systems, an OS-provided Qt6 install can be
# found by the dynamic linker *before* the newer Qt6 bundled inside the
# PySide6 wheel, causing an "undefined symbol" crash on import. Prepending
# PySide6's own Qt lib dir to LD_LIBRARY_PATH makes the linker resolve the
# matching version first.
#
# Usage: run.sh [PROJECT]
#   PROJECT   optional .phidler project (or .py script) to open on launch;
#             omit it to get the usual startup picker. All arguments are
#             forwarded to `python -m phidler`, so Qt options work too.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"

# First-run setup: if the project venv isn't there yet, create it and install
# phidler into it, mirroring the documented setup (python3 -m venv .venv;
# pip install -e ".[dev]"). This must run before the QT_LIB_DIR find below,
# which would otherwise fail (set -e) on the missing directory. FDTD support
# additionally needs the sibling photonfdtd installed — a separate step, see
# the README — so this bootstrap keeps to the base + dev install.
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

exec "$PYTHON" -m phidler "$@"
