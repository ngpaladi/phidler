#!/usr/bin/env bash
# Launcher for Phidler. Activates the project venv and works around a Qt
# library conflict: this machine has an apt-installed Qt6 6.4.2 under
# /usr/lib/x86_64-linux-gnu which the dynamic linker finds *before* the
# newer Qt6 bundled inside the PySide6 wheel, causing an
# "undefined symbol" crash on import. Prepending PySide6's own Qt lib dir
# to LD_LIBRARY_PATH makes the linker resolve the matching version first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"
QT_LIB_DIR="$VENV_DIR/lib/python3.12/site-packages/PySide6/Qt/lib"

export LD_LIBRARY_PATH="$QT_LIB_DIR:${LD_LIBRARY_PATH:-}"

exec "$PYTHON" -m phidler "$@"
