#!/usr/bin/env bash
# Launcher for Phidler. Activates the project venv and works around a Qt
# library conflict: on some Linux systems, an OS-provided Qt6 install can be
# found by the dynamic linker *before* the newer Qt6 bundled inside the
# PySide6 wheel, causing an "undefined symbol" crash on import. Prepending
# PySide6's own Qt lib dir to LD_LIBRARY_PATH makes the linker resolve the
# matching version first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"
QT_LIB_DIR="$(find "$VENV_DIR/lib" -maxdepth 1 -name 'python3.*')/site-packages/PySide6/Qt/lib"

export LD_LIBRARY_PATH="$QT_LIB_DIR:${LD_LIBRARY_PATH:-}"

exec "$PYTHON" -m phidler "$@"
