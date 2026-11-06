#!/usr/bin/env bash
# Build Phidler.flatpak, a single-file bundle you can install with
#   flatpak install --user Phidler.flatpak     (or without --user, system-wide)
#
# Needs flatpak and the Flathub builder (flatpak install flathub org.flatpak.Builder).
# The build tree and repo go under $WORKDIR (default ~/.cache/phidler-flatpak;
# it must be a path the builder sandbox sees at the same place, so not /tmp or
# /var/tmp),
# since the bundled CUDA wheels make the build several GB.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${WORKDIR:-$HOME/.cache/phidler-flatpak}"
OUT="${1:-$PWD/Phidler.flatpak}"
mkdir -p "$WORKDIR"
# The runtime and SDK, installed from the host (the builder's own sandbox can't
# see a --user remote). A no-op when they're already there.
flatpak install --user -y --noninteractive flathub \
    org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08 >/dev/null
# The builder runs host-side flatpak commands with the sandbox's XDG_DATA_HOME,
# which would hide the --user installation; point them at the real one.
flatpak run --env=FLATPAK_USER_DIR="${FLATPAK_USER_DIR:-$HOME/.local/share/flatpak}" \
    --command=flatpak-builder org.flatpak.Builder \
    --user --force-clean --disable-rofiles-fuse \
    --state-dir="$WORKDIR/state" --repo="$WORKDIR/repo" \
    "$WORKDIR/build" "$HERE/io.github.ngpaladi.Phidler.yml"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$WORKDIR/repo" "$OUT" io.github.ngpaladi.Phidler
echo "Built $OUT"
