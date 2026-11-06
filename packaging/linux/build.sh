#!/usr/bin/env bash
# Build Phidler's .deb and .rpm.
#
#   packaging/linux/build.sh              # -> ./phidler_<ver>_amd64.deb, ./phidler-<ver>-1.x86_64.rpm
#   GPU=1 packaging/linux/build.sh        # also bundle JAX's CUDA 12 build (adds ~2 GB)
#   FORMATS=deb packaging/linux/build.sh  # just one format
#
# Needs uv (fetches the relocatable python-build-standalone CPython) and nfpm
# (writes both package formats without dpkg-deb or rpmbuild); nfpm is downloaded
# into the build directory when it isn't on PATH. Like the AppImage and the
# Flatpak, the Python stack is pip-installed from the network at build time.
#
# The packages install that CPython, with phidler and the FDTD stack
# (photonfdtd, JAX, matplotlib) inside it, to /opt/phidler, plus a /usr/bin/phidler
# launcher and the desktop entry, icon and AppStream metainfo.
set -euo pipefail

PYVER="${PHIDLER_PYTHON:-3.12}"
NFPM_VERSION="${NFPM_VERSION:-2.48.0}"
FORMATS="${FORMATS:-deb rpm}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LINUX_DIR="$REPO_ROOT/packaging/linux"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build/linux}"
STAGE="$BUILD_DIR/stage"
PREFIX=/opt/phidler

case "$(uname -m)" in
    x86_64) NFPM_ARCH=amd64 ;;
    aarch64) NFPM_ARCH=arm64 ;;
    *) echo "error: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv not found — see https://docs.astral.sh/uv/ (pip install uv)." >&2
    exit 1
fi

PHIDLER_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/pyproject.toml" | head -1)"
echo "Packaging phidler $PHIDLER_VERSION for $NFPM_ARCH"

rm -rf "$STAGE"
mkdir -p "$STAGE" "$BUILD_DIR/python"

# 1. A relocatable CPython. uv's managed Pythons are python-build-standalone
#    builds, which run from any prefix, so a copy of one becomes /opt/phidler.
uv python install "$PYVER" --install-dir "$BUILD_DIR/python" --no-bin
PYSRC="$(find "$BUILD_DIR/python" -maxdepth 1 -type d -name "cpython-$PYVER*-linux-*" | sort | tail -1)"
[ -n "$PYSRC" ] || { echo "error: uv didn't install CPython $PYVER" >&2; exit 1; }
mkdir -p "$STAGE$PREFIX"
cp -a "$PYSRC/." "$STAGE$PREFIX/"
PY="$STAGE$PREFIX/bin/python3"
# The bundle is private to the app, so it isn't "externally managed".
rm -f "$STAGE$PREFIX"/lib/python3*/EXTERNALLY-MANAGED

# 2. The app and the FDTD stack (keep in sync with the [fdtd] group in
#    pyproject.toml and the Flatpak manifest).
REQS=("photonfdtd>=0.15.0" "matplotlib>=3.7")
[ "${GPU:-0}" = 1 ] && REQS+=("jax[cuda12]")
export PIP_NO_CACHE_DIR=1
uv pip install --python "$PY" --no-cache "${REQS[@]}" "$REPO_ROOT"

# 3. Make the tree relocatable to /opt/phidler: console scripts carry the staging
#    path in their shebangs. Drop byte-code compiled at the staging path; Python
#    can't rewrite it under a root-owned /opt, so recompile it below with the
#    final path baked in.
grep -rlI "^#!$STAGE$PREFIX/bin/" "$STAGE$PREFIX/bin" 2>/dev/null \
    | xargs -r sed -i "1s|^#!$STAGE$PREFIX/bin/|#!$PREFIX/bin/|"
find "$STAGE$PREFIX" -name __pycache__ -type d -prune -exec rm -rf {} +
# (compileall exits non-zero over a few deliberately invalid test files in
# third-party packages; those just stay uncompiled.)
"$PY" -m compileall -q -j0 -s "$STAGE" -p / "$STAGE$PREFIX/lib" >/dev/null || true

# 4. Desktop entry, named after the app ID like the icon and metainfo.
sed "s/^Icon=.*/Icon=io.github.ngpaladi.Phidler/" \
    "$REPO_ROOT/packaging/appimage/phidler/phidler.desktop" > "$STAGE/io.github.ngpaladi.Phidler.desktop"

# 5. Package.
NFPM="$(command -v nfpm || true)"
if [ -z "$NFPM" ]; then
    NFPM="$BUILD_DIR/nfpm"
    if [ ! -x "$NFPM" ]; then
        nfpm_arch=$([ "$NFPM_ARCH" = amd64 ] && echo x86_64 || echo arm64)
        curl -fsSL "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/nfpm_${NFPM_VERSION}_Linux_${nfpm_arch}.tar.gz" \
            | tar -xz -C "$BUILD_DIR" nfpm
    fi
fi
# nfpm expands environment variables only in a few fields, so fill in the
# config's ${...} paths here.
export NFPM_ARCH PHIDLER_VERSION STAGE LINUX_DIR REPO_ROOT
python3 -c 'import os, sys; sys.stdout.write(os.path.expandvars(sys.stdin.read()))' \
    < "$LINUX_DIR/nfpm.yaml" > "$BUILD_DIR/nfpm.yaml"
cd "$REPO_ROOT"
for fmt in $FORMATS; do
    "$NFPM" package --config "$BUILD_DIR/nfpm.yaml" --packager "$fmt" --target "$REPO_ROOT/"
done
ls -l "$REPO_ROOT"/phidler[_-]"$PHIDLER_VERSION"*.{deb,rpm} 2>/dev/null || true
