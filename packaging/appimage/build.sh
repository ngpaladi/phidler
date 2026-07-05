#!/usr/bin/env bash
# Build the Phidler AppImage with python-appimage.
#
#   pip install python-appimage
#   packaging/appimage/build.sh            # -> ./Phidler-<arch>.AppImage
#
# python-appimage takes a recipe directory (requirements.txt + .desktop + icon +
# entrypoint), downloads a relocatable manylinux CPython as a base AppImage,
# pip-installs the requirements into it, and repackages. No FUSE needed — it
# extracts rather than mounts — so this works on CI runners too.
set -euo pipefail

PYVER="${PHIDLER_APPIMAGE_PYTHON:-3.12}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RECIPE="$REPO_ROOT/packaging/appimage/phidler"

if ! command -v python-appimage >/dev/null 2>&1; then
    echo "error: python-appimage not found — run 'pip install python-appimage'." >&2
    exit 1
fi

# Build against a copy of the recipe so we can append the current checkout (an
# absolute path — it differs between a local build and CI) without mutating the
# committed requirements.txt. A bare path (no "[fdtd]") on purpose: python-appimage
# installs each requirement through a shell, which would glob-expand the brackets;
# the FDTD extras are listed explicitly in requirements.txt instead.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -a "$RECIPE/." "$WORK/"
echo "${REPO_ROOT}" >> "$WORK/requirements.txt"

# python-appimage writes the AppImage into the current directory.
cd "$REPO_ROOT"
python-appimage build app -p "$PYVER" "$WORK"

echo "Built: $REPO_ROOT/Phidler-$(uname -m).AppImage"
