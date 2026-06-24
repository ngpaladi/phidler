from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from phidler.model.document import ProjectSettings
from phidler.pdk_catalog import list_cross_section_names
from phidler.waveguide_calc import DISCLAIMER, PLATFORM_PRESETS, suggested_waveguide_width


class ProjectSettingsDialog(QDialog):
    """Shown on File > New and on app startup. Lets the user pick a
    material platform (or enter custom indices/thickness), a design
    wavelength, and a cross_section to default routing to — and shows a
    live-updating suggested single-mode waveguide width, clearly labeled
    as an approximation (see waveguide_calc.DISCLAIMER), not a substitute
    for real mode-solving."""

    def __init__(self, initial: ProjectSettings | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project Settings")
        initial = initial or ProjectSettings()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.platform_combo = QComboBox()
        self.platform_combo.addItems(PLATFORM_PRESETS.keys())
        self.platform_combo.setCurrentText(initial.platform_name)
        self.platform_combo.currentTextChanged.connect(self._on_platform_changed)
        form.addRow("Platform", self.platform_combo)

        self.core_index_spin = QDoubleSpinBox()
        self.core_index_spin.setRange(1.0, 10.0)
        self.core_index_spin.setDecimals(3)
        self.core_index_spin.setValue(initial.core_index)
        self.core_index_spin.valueChanged.connect(self._update_suggestion)
        form.addRow("Core index", self.core_index_spin)

        self.clad_index_spin = QDoubleSpinBox()
        self.clad_index_spin.setRange(1.0, 10.0)
        self.clad_index_spin.setDecimals(3)
        self.clad_index_spin.setValue(initial.clad_index)
        self.clad_index_spin.valueChanged.connect(self._update_suggestion)
        form.addRow("Cladding index", self.clad_index_spin)

        self.thickness_spin = QDoubleSpinBox()
        self.thickness_spin.setRange(0.001, 100.0)
        self.thickness_spin.setDecimals(3)
        self.thickness_spin.setSuffix(" µm")
        self.thickness_spin.setValue(initial.thickness_um)
        self.thickness_spin.valueChanged.connect(self._update_suggestion)
        form.addRow("Core thickness", self.thickness_spin)

        self.wavelength_spin = QDoubleSpinBox()
        self.wavelength_spin.setRange(0.1, 20.0)
        self.wavelength_spin.setDecimals(3)
        self.wavelength_spin.setSuffix(" µm")
        self.wavelength_spin.setValue(initial.wavelength_um)
        self.wavelength_spin.valueChanged.connect(self._update_suggestion)
        form.addRow("Design wavelength", self.wavelength_spin)

        self.cross_section_combo = QComboBox()
        self.cross_section_combo.addItems(list_cross_section_names())
        self.cross_section_combo.setCurrentText(initial.cross_section)
        form.addRow("Default cross-section", self.cross_section_combo)

        self.suggestion_label = QLabel()
        self.suggestion_label.setWordWrap(True)
        form.addRow("Suggested width", self.suggestion_label)

        disclaimer_label = QLabel(DISCLAIMER)
        disclaimer_label.setWordWrap(True)
        disclaimer_label.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(disclaimer_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_suggestion()

    def _on_platform_changed(self, name: str) -> None:
        preset = PLATFORM_PRESETS.get(name)
        if preset is None:
            return
        self.core_index_spin.setValue(preset.core_index)
        self.clad_index_spin.setValue(preset.clad_index)
        self.thickness_spin.setValue(preset.thickness_um)
        self.cross_section_combo.setCurrentText(preset.cross_section)

    def _update_suggestion(self) -> None:
        try:
            suggested, cutoff = suggested_waveguide_width(
                thickness_um=self.thickness_spin.value(),
                core_index=self.core_index_spin.value(),
                clad_index=self.clad_index_spin.value(),
                wavelength_um=self.wavelength_spin.value(),
            )
        except ValueError as exc:
            self.suggestion_label.setText(f"Cannot estimate: {exc}")
            return
        self.suggestion_label.setText(
            f"~{suggested * 1000:.0f} nm (single-mode cutoff ~{cutoff * 1000:.0f} nm) — see note below"
        )

    def result_settings(self) -> ProjectSettings:
        return ProjectSettings(
            platform_name=self.platform_combo.currentText(),
            core_index=self.core_index_spin.value(),
            clad_index=self.clad_index_spin.value(),
            thickness_um=self.thickness_spin.value(),
            wavelength_um=self.wavelength_spin.value(),
            cross_section=self.cross_section_combo.currentText(),
        )
