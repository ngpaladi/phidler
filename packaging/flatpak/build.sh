#!/usr/bin/env bash
# Build the Phidler Flatpak and export it as a single-file bundle.
#
#   # needs flatpak + flatpak-builder (apt/dnf install flatpak-builder), or
#   # without root: flatpak install --user flathub org.flatpak.Builder
#   packaging/flatpak/build.sh             # -> ./Phidler-<arch>.flatpak
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
OUT="$REPO_ROOT/build/flatpak"
BUNDLE="$REPO_ROOT/Phidler-$(uname -m).flatpak"

if ! command -v flatpak >/dev/null 2>&1; then
    echo "error: flatpak not found." >&2
    exit 1
fi
# Prefer a native flatpak-builder; fall back to the Flathub-packaged one.
if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER=(flatpak-builder)
elif flatpak info --user org.flatpak.Builder >/dev/null 2>&1 || flatpak info --system org.flatpak.Builder >/dev/null 2>&1; then
    BUILDER=(flatpak run org.flatpak.Builder)
else
    echo "error: flatpak-builder not found — install it (apt/dnf install flatpak-builder," >&2
    echo "       or: flatpak install --user flathub org.flatpak.Builder)." >&2
    exit 1
fi

flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

# Log which photonfdtd commit the manifest's bare git URL resolves to, for
# provenance (same as the AppImage build).
PHOTONFDTD_SHA="$(git ls-remote https://github.com/ngpaladi/photonfdtd.git HEAD 2>/dev/null | awk 'NR==1{print $1}')"
[ -n "$PHOTONFDTD_SHA" ] && echo "Bundling photonfdtd at latest GitHub main commit ${PHOTONFDTD_SHA}"

# Run from the repo root so flatpak-builder's state dir (.flatpak-builder/) lands
# there, not wherever the script was invoked from.
cd "$REPO_ROOT"
"${BUILDER[@]}" --user --force-clean --disable-rofiles-fuse \
    --install-deps-from=flathub \
    --repo="$OUT/repo" "$OUT/build" "$MANIFEST"

flatpak build-bundle \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$OUT/repo" "$BUNDLE" "$APP_ID"

echo "Built: $BUNDLE"
