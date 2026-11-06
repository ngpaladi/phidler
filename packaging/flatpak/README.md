# Phidler Flatpak

A Flatpak of Phidler with the full FDTD stack, including JAX's CUDA 12 build,
so simulations and the Train tab run on an NVIDIA GPU.

## Build

```
flatpak install flathub org.flatpak.Builder          # once
packaging/flatpak/build.sh                            # -> ./Phidler.flatpak
```

`build.sh` installs the freedesktop 25.08 runtime and SDK (per-user) if they're
missing, builds into `$WORKDIR` (default `~/.cache/phidler-flatpak`; the build
needs ~17 GB, so point it at a roomy disk, e.g.
`WORKDIR=/mnt/Storage/phidler-flatpak`), and writes a single-file bundle. The
Python packages are pip-installed from PyPI during the build, which needs
network access in the build sandbox; fine for a local bundle, not for Flathub.
The bundle step compresses ~6 GB of CUDA libraries and takes about half an hour.

## Install and run

```
flatpak install --user Phidler.flatpak               # or system-wide without --user
flatpak run io.github.ngpaladi.Phidler
```

It needs about 6.5 GB installed. For the GPU, Flatpak also needs the NVIDIA
driver extension matching your host driver
(`org.freedesktop.Platform.GL.nvidia-<version>`); `flatpak update` installs it
from Flathub. Check that JAX really uses the GPU with **Check JAX GPU…** in the
FDTD window, or:

```
flatpak run --command=python3 io.github.ngpaladi.Phidler -m phidler.jax_check
```

The app sees your home directory (projects, GDS, custom components), the
network (SSH/nereid/Tidy3D remote runs) and your SSH agent.
