import math

import pytest

from phidler.waveguide_calc import (
    PLATFORM_PRESETS,
    single_mode_width_cutoff,
    slab_te0_effective_index,
    suggested_waveguide_width,
)


def test_slab_effective_index_for_known_soi_platform():
    """220nm silicon-on-insulator at 1550nm — value confirmed by direct
    calculation during development, used here as the regression anchor."""
    n_eff = slab_te0_effective_index(thickness_um=0.220, core_index=3.45, clad_index=1.44, wavelength_um=1.55)
    assert math.isclose(n_eff, 2.8217, abs_tol=1e-3)


def test_effective_index_is_between_clad_and_core():
    n_eff = slab_te0_effective_index(thickness_um=0.220, core_index=3.45, clad_index=1.44, wavelength_um=1.55)
    assert 1.44 < n_eff < 3.45


def test_effective_index_rejects_non_guiding_indices():
    with pytest.raises(ValueError):
        slab_te0_effective_index(thickness_um=0.220, core_index=1.4, clad_index=1.44, wavelength_um=1.55)


def test_effective_index_rejects_nonpositive_thickness():
    with pytest.raises(ValueError):
        slab_te0_effective_index(thickness_um=0.0, core_index=3.45, clad_index=1.44, wavelength_um=1.55)


def test_suggested_width_for_soi_matches_verified_calculation():
    suggested, cutoff = suggested_waveguide_width(
        thickness_um=0.220, core_index=3.45, clad_index=1.44, wavelength_um=1.55
    )
    assert math.isclose(cutoff, 0.3194, abs_tol=2e-3)
    assert math.isclose(suggested, cutoff * 0.9, abs_tol=1e-9)


def test_suggested_width_for_sin_matches_verified_calculation():
    suggested, cutoff = suggested_waveguide_width(
        thickness_um=0.400, core_index=2.0, clad_index=1.44, wavelength_um=1.55
    )
    assert math.isclose(cutoff, 0.7852, abs_tol=2e-3)


def test_higher_index_contrast_gives_narrower_single_mode_width():
    """Sanity check on the physics direction, not just a frozen number:
    higher core/clad contrast confines more tightly, so the single-mode
    width cutoff should shrink, not grow."""
    _, cutoff_low_contrast = suggested_waveguide_width(0.4, 2.0, 1.44, 1.55)
    _, cutoff_high_contrast = suggested_waveguide_width(0.22, 3.45, 1.44, 1.55)
    assert cutoff_high_contrast < cutoff_low_contrast


def test_thicker_slab_gives_narrower_single_mode_width():
    """A thicker vertical slab confines the mode more strongly (n_eff
    closer to core index), which narrows the lateral single-mode cutoff."""
    _, cutoff_thin = suggested_waveguide_width(0.150, 3.45, 1.44, 1.55)
    _, cutoff_thick = suggested_waveguide_width(0.300, 3.45, 1.44, 1.55)
    assert cutoff_thick < cutoff_thin


def test_platform_presets_have_valid_guiding_indices():
    for preset in PLATFORM_PRESETS.values():
        assert preset.core_index > preset.clad_index
        assert preset.thickness_um > 0
        # must not raise for any preset's own defaults
        suggested_waveguide_width(preset.thickness_um, preset.core_index, preset.clad_index, 1.55)


def test_single_mode_width_cutoff_matches_naive_symmetric_slab_formula():
    """When effective_core_index == bare core index (i.e. skip the
    vertical EIM step), this must reduce to the textbook symmetric-slab
    V<pi/2 cutoff formula: w_max = lambda / (2*sqrt(n_core^2 - n_clad^2))."""
    wavelength_um = 1.55
    core_index = 3.45
    clad_index = 1.44
    cutoff = single_mode_width_cutoff(core_index, clad_index, wavelength_um)
    expected = wavelength_um / (2 * math.sqrt(core_index**2 - clad_index**2))
    assert math.isclose(cutoff, expected, rel_tol=1e-9)
