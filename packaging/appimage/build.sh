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

# Bundle the *latest* photonfdtd from GitHub. The committed requirements.txt uses
# a bare `git+…photonfdtd.git` (no ref); resolve main's current commit here and
# pin it in the working copy as `…photonfdtd.git@<sha>`. This makes "latest from
# GitHub" explicit and reproducible (the exact SHA is printed and baked into the
# build), and defeats any pip wheel-cache reuse — a fresh commit is a fresh
# install target every night. The `@<sha>` suffix is space-free, so it stays
# compatible with python-appimage's per-line `pip install` (see requirements.txt).
PHOTONFDTD_REPO="https://github.com/ngpaladi/photonfdtd.git"
PHOTONFDTD_SHA="$(git ls-remote "$PHOTONFDTD_REPO" HEAD 2>/dev/null | awk 'NR==1{print $1}')"
if [ -n "$PHOTONFDTD_SHA" ]; then
    echo "Pinning photonfdtd to latest GitHub main commit ${PHOTONFDTD_SHA}"
    sed -i \
        "s#^git+https://github.com/ngpaladi/photonfdtd.git.*#git+https://github.com/ngpaladi/photonfdtd.git@${PHOTONFDTD_SHA}#" \
        "$WORK/requirements.txt"
else
    echo "warning: could not resolve photonfdtd HEAD (no network?); using the bare git URL (still fetches main HEAD)." >&2
fi

echo "${REPO_ROOT}" >> "$WORK/requirements.txt"

# python-appimage writes the AppImage into the current directory. Disable pip's
# cache so the photonfdtd build is never served from a stale cached wheel.
export PIP_NO_CACHE_DIR=1
cd "$REPO_ROOT"
python-appimage build app -p "$PYVER" "$WORK"

echo "Built: $REPO_ROOT/Phidler-$(uname -m).AppImage"
