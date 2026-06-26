import math

from phidler.model.document import ProjectSettings
from phidler.panels.project_settings_dialog import ProjectSettingsDialog


def test_dialog_defaults_to_silicon_soi(qapp):
    dialog = ProjectSettingsDialog()
    assert dialog.platform_combo.currentText() == "Silicon (SOI)"
    assert math.isclose(dialog.core_index_spin.value(), 3.45)
    assert math.isclose(dialog.thickness_spin.value(), 0.220)
    assert math.isclose(dialog.clad_thickness_spin.value(), 2.0)
    assert dialog.cross_section_combo.currentText() == "strip"


def test_switching_platform_does_not_touch_clad_thickness(qapp):
    """Cladding thickness is a wafer/process choice, not a material
    property — unlike core_index/clad_index/thickness_um, it must not
    get silently overwritten just because the user picked a different
    platform preset."""
    dialog = ProjectSettingsDialog()
    dialog.clad_thickness_spin.setValue(5.5)
    dialog.platform_combo.setCurrentText("Silicon Nitride (SiN)")
    assert math.isclose(dialog.clad_thickness_spin.value(), 5.5)


def test_dialog_seeded_from_existing_settings(qapp):
    existing = ProjectSettings(
        platform_name="Silicon Nitride (SiN)",
        core_index=2.0,
        clad_index=1.44,
        thickness_um=0.4,
        wavelength_um=1.31,
        cross_section="nitride",
    )
    dialog = ProjectSettingsDialog(initial=existing)
    assert dialog.platform_combo.currentText() == "Silicon Nitride (SiN)"
    assert math.isclose(dialog.wavelength_spin.value(), 1.31)
    assert dialog.cross_section_combo.currentText() == "nitride"


def test_switching_platform_refills_fields(qapp):
    dialog = ProjectSettingsDialog()
    dialog.platform_combo.setCurrentText("Silicon Nitride (SiN)")
    assert math.isclose(dialog.core_index_spin.value(), 2.0)
    assert math.isclose(dialog.thickness_spin.value(), 0.4)
    assert dialog.cross_section_combo.currentText() == "nitride"


def test_suggestion_label_updates_live_as_fields_change(qapp):
    dialog = ProjectSettingsDialog()
    text_before = dialog.suggestion_label.text()
    dialog.thickness_spin.setValue(0.4)  # changes the implied suggestion
    assert dialog.suggestion_label.text() != text_before
    assert "nm" in dialog.suggestion_label.text()


def test_suggestion_label_handles_invalid_indices_without_raising(qapp):
    dialog = ProjectSettingsDialog()
    dialog.clad_index_spin.setValue(5.0)
    dialog.core_index_spin.setValue(2.0)  # now clad > core, non-guiding
    assert "Cannot estimate" in dialog.suggestion_label.text()


def test_result_settings_reflects_current_field_values(qapp):
    dialog = ProjectSettingsDialog()
    dialog.platform_combo.setCurrentText("Silicon Nitride (SiN)")
    dialog.wavelength_spin.setValue(1.31)
    dialog.clad_thickness_spin.setValue(3.5)

    settings = dialog.result_settings()
    assert settings.platform_name == "Silicon Nitride (SiN)"
    assert math.isclose(settings.core_index, 2.0)
    assert math.isclose(settings.wavelength_um, 1.31)
    assert math.isclose(settings.clad_thickness_um, 3.5)
    assert settings.cross_section == "nitride"


def test_infinite_cladding_checkbox_round_trips_and_greys_out_thickness(qapp):
    dialog = ProjectSettingsDialog()
    assert dialog.clad_infinite_check.isChecked() is False
    assert dialog.clad_thickness_spin.isEnabled() is True

    dialog.clad_infinite_check.setChecked(True)
    # The thickness value is ignored in infinite mode, so it greys out.
    assert dialog.clad_thickness_spin.isEnabled() is False
    assert dialog.result_settings().clad_infinite is True

    dialog.clad_infinite_check.setChecked(False)
    assert dialog.clad_thickness_spin.isEnabled() is True
    assert dialog.result_settings().clad_infinite is False


def test_dialog_seeded_with_infinite_cladding_disables_thickness(qapp):
    existing = ProjectSettings(clad_infinite=True)
    dialog = ProjectSettingsDialog(initial=existing)
    assert dialog.clad_infinite_check.isChecked() is True
    assert dialog.clad_thickness_spin.isEnabled() is False
