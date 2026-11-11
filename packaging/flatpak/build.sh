#!/usr/bin/env bash
# Build the Phidler Flatpak and export it as a single-file bundle.
#
#   # needs flatpak + flatpak-builder (apt/dnf install flatpak-builder), or
#   # without root: flatpak install --user flathub org.flatpak.Builder
#   packaging/flatpak/build.sh             # -> ./Phidler-<arch>.flatpak
#   GPU=1 packaging/flatpak/build.sh       # also bundle JAX's CUDA 12 build
#                                          #   -> ./Phidler-<arch>-gpu.flatpak (~2.3 GB)
#   BUILD_DIR=/big/disk/phidler-flatpak packaging/flatpak/build.sh
#                                          # build tree elsewhere (it's ~17 GB with GPU=1)
#
# flatpak-builder fetches the KDE runtime/SDK from Flathub (a per-user `flathub`
# remote is added if missing), pip-installs photonfdtd + the FDTD extras + this
# checkout into /app (see the manifest), and exports the result to a local OSTree
# repo, which is then packed into a .flatpak bundle. The bundle records Flathub as
# its runtime repo, so `flatpak install Phidler-x86_64.flatpak` pulls the runtime
# automatically.
set -euo pipefail

APP_ID="io.github.ngpaladi.Phidler"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/packaging/flatpak/$APP_ID.yml"
OUT="${BUILD_DIR:-$REPO_ROOT/build/flatpak}"
if [ "${GPU:-0}" = 1 ]; then
    BUNDLE="$REPO_ROOT/Phidler-$(uname -m)-gpu.flatpak"
    # Same manifest with JAX's CUDA build added to the FDTD pip line, written
    # next to the original so its relative source path still resolves.
    GPU_MANIFEST="$REPO_ROOT/packaging/flatpak/.$APP_ID.gpu.yml"
    sed 's|"photonfdtd>=0.16.0" "matplotlib>=3.7"|"photonfdtd>=0.16.0" "matplotlib>=3.7" "jax[cuda12]"|' \
        "$MANIFEST" > "$GPU_MANIFEST"
    grep -q 'jax\[cuda12\]' "$GPU_MANIFEST" || { echo "error: couldn't add jax[cuda12] to the manifest" >&2; exit 1; }
    trap 'rm -f "$GPU_MANIFEST"' EXIT
    MANIFEST="$GPU_MANIFEST"
else
    BUNDLE="$REPO_ROOT/Phidler-$(uname -m).flatpak"
fi

if ! command -v flatpak >/dev/null 2>&1; then
    echo "error: flatpak not found." >&2
    exit 1
fi
# Prefer a native flatpak-builder; fall back to the Flathub-packaged one.
if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER=(flatpak-builder)
elif flatpak info --user org.flatpak.Builder >/dev/null 2>&1 || flatpak info --system org.flatpak.Builder >/dev/null 2>&1; then
    # The sandboxed builder runs its host-side flatpak commands with the
    # sandbox's XDG_DATA_HOME, which hides the --user installation (so it can't
    # find the SDK it just installed); point it at the real one.
    BUILDER=(flatpak run --env=FLATPAK_USER_DIR="${FLATPAK_USER_DIR:-$HOME/.local/share/flatpak}"
             org.flatpak.Builder)
else
    echo "error: flatpak-builder not found — install it (apt/dnf install flatpak-builder," >&2
    echo "       or: flatpak install --user flathub org.flatpak.Builder)." >&2
    exit 1
fi

flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

# The runtime and SDK the manifest names, installed from the host: the
# sandboxed builder can't see a --user remote for --install-deps-from.
RUNTIME_VERSION="$(awk -F'"' '/^runtime-version:/{print $2}' "$MANIFEST")"
flatpak install --user -y --noninteractive flathub \
    "org.kde.Platform//$RUNTIME_VERSION" "org.kde.Sdk//$RUNTIME_VERSION" >/dev/null

# Run from the repo root so flatpak-builder's state dir (.flatpak-builder/) lands
# there, not wherever the script was invoked from.
cd "$REPO_ROOT"
"${BUILDER[@]}" --user --force-clean --disable-rofiles-fuse \
    --state-dir="$OUT/state" --repo="$OUT/repo" "$OUT/build" "$MANIFEST"

flatpak build-bundle \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$OUT/repo" "$BUNDLE" "$APP_ID"

echo "Built: $BUNDLE"
