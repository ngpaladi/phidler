from __future__ import annotations

import math
from dataclasses import dataclass

DISCLAIMER = (
    "Estimated via the effective-index method (EIM): a 1D slab solve for "
    "vertical confinement, then a second 1D slab cutoff for the lateral "
    "direction. This is a real, standard approximation technique — not a "
    "made-up number — but it is NOT a full 2D mode solve, and EIM is known "
    "to underestimate the practical single-mode width for high-index-"
    "contrast platforms (verified against silicon: this method gives "
    "~319nm for 220nm SOI at 1550nm, versus the ~450-500nm commonly used "
    "in practice — for reference, the generic PDK's own 'strip' "
    "cross_section defaults to 500nm width). Treat this as a rough "
    "starting point, not a substitute for a real mode solver or your "
    "foundry's PDK documentation."
)


@dataclass(frozen=True)
class PlatformPreset:
    name: str
    core_index: float
    clad_index: float
    thickness_um: float
    cross_section: str


PLATFORM_PRESETS: dict[str, PlatformPreset] = {
    "Silicon (SOI)": PlatformPreset("Silicon (SOI)", core_index=3.45, clad_index=1.44, thickness_um=0.220, cross_section="strip"),
    "Silicon Nitride (SiN)": PlatformPreset(
        "Silicon Nitride (SiN)", core_index=2.0, clad_index=1.44, thickness_um=0.400, cross_section="nitride"
    ),
    "Custom": PlatformPreset("Custom", core_index=3.45, clad_index=1.44, thickness_um=0.220, cross_section="strip"),
}


def slab_te0_effective_index(thickness_um: float, core_index: float, clad_index: float, wavelength_um: float) -> float:
    """Effective index of the fundamental (TE0) mode of a symmetric 1D
    slab waveguide, solved from the standard transcendental dispersion
    relation via bisection (no closed form exists in general — this is
    the textbook equation itself, not an additional approximation layer):

        u = atan(w / u),  where
        u = (k0*t/2) * sqrt(core_index^2 - n_eff^2)
        w = (k0*t/2) * sqrt(n_eff^2 - clad_index^2)

    Requires core_index > clad_index (a guided mode must exist) and
    thickness_um > 0.
    """
    if core_index <= clad_index:
        raise ValueError("core_index must exceed clad_index for a guided mode to exist")
    if thickness_um <= 0:
        raise ValueError("thickness_um must be positive")

    k0 = 2 * math.pi / wavelength_um
    lo, hi = clad_index + 1e-9, core_index - 1e-9

    def f(n_eff: float) -> float:
        u = k0 * thickness_um / 2 * math.sqrt(max(core_index**2 - n_eff**2, 1e-15))
        w = k0 * thickness_um / 2 * math.sqrt(max(n_eff**2 - clad_index**2, 1e-15))
        return u - math.atan(w / u)

    f_lo = f(lo)
    for _ in range(200):
        mid = (lo + hi) / 2
        if f_lo * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
            f_lo = f(mid)
    return (lo + hi) / 2


def single_mode_width_cutoff(effective_core_index: float, clad_index: float, wavelength_um: float) -> float:
    """The symmetric-slab single-mode cutoff width: the maximum width
    before a second lateral mode (TE1) is supported, per V = pi/2."""
    return wavelength_um / (2 * math.sqrt(max(effective_core_index**2 - clad_index**2, 1e-15)))


def suggested_waveguide_width(
    thickness_um: float, core_index: float, clad_index: float, wavelength_um: float, margin: float = 0.9
) -> tuple[float, float]:
    """Two-step effective-index estimate. Returns (suggested_width_um,
    cutoff_width_um) — the cutoff is the EIM single-mode limit; the
    suggestion applies `margin` (default 90%) to stay comfortably under
    it rather than running right at the edge of multimode behavior."""
    n_eff_vertical = slab_te0_effective_index(thickness_um, core_index, clad_index, wavelength_um)
    cutoff = single_mode_width_cutoff(n_eff_vertical, clad_index, wavelength_um)
    return cutoff * margin, cutoff
