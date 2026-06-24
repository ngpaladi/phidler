#!/usr/bin/env bash
# Runs the test suite headlessly with the same Qt library fix run.sh uses.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
QT_LIB_DIR="$VENV_DIR/lib/python3.12/site-packages/PySide6/Qt/lib"

export LD_LIBRARY_PATH="$QT_LIB_DIR:${LD_LIBRARY_PATH:-}"
export QT_QPA_PLATFORM=offscreen

exec "$VENV_DIR/bin/python3" -m pytest "$SCRIPT_DIR/tests" "$@"
