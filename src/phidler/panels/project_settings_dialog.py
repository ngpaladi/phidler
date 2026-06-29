from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from phidler.model.document import EtchLayer, ProjectSettings
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

        self.clad_thickness_spin = QDoubleSpinBox()
        self.clad_thickness_spin.setRange(0.001, 10_000.0)
        self.clad_thickness_spin.setDecimals(3)
        self.clad_thickness_spin.setSuffix(" µm")
        self.clad_thickness_spin.setValue(initial.clad_thickness_um)
        self.clad_thickness_spin.setToolTip(
            "A generic default, not sourced from any specific foundry process. "
            "Doesn't affect the suggested-width estimate above (which assumes "
            "semi-infinite cladding) — it's the vertical simulation domain extent "
            "used by FDTD Simulation's mode solver and propagation runs, unless "
            "'Assume infinite cladding depth' is checked below."
        )
        form.addRow("Cladding thickness", self.clad_thickness_spin)

        self.clad_infinite_check = QCheckBox("Assume infinite cladding depth")
        self.clad_infinite_check.setChecked(initial.clad_infinite)
        self.clad_infinite_check.setToolTip(
            "Ignore the cladding thickness above and use an effectively "
            "semi-infinite cladding for the FDTD mode solver and propagation "
            "runs, so the guided mode decays fully before reaching the domain "
            "boundary. Costs more vertical grid; use it to check confinement "
            "without picking a specific cladding thickness."
        )
        self.clad_infinite_check.toggled.connect(self._on_clad_infinite_toggled)
        form.addRow("", self.clad_infinite_check)
        self._on_clad_infinite_toggled(self.clad_infinite_check.isChecked())

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

        # Partial-etch (rib/slab) layers. Each row is a drawing layer whose
        # geometry is core material only `slab thickness` tall (the rest of the
        # full core thickness above it is cladding) — so FDTD and the mode solver
        # see a ridge-over-slab rib instead of a fully-etched strip.
        etch_widget = QWidget()
        etch_layout = QVBoxLayout(etch_widget)
        etch_layout.setContentsMargins(0, 0, 0, 0)
        self.etch_table = QTableWidget(0, 3)
        self.etch_table.setHorizontalHeaderLabels(["Layer", "Datatype", "Slab thickness"])
        self.etch_table.setMinimumHeight(96)  # header + a couple of rows stay visible
        self.etch_table.setMaximumHeight(140)
        self.etch_table.verticalHeader().setVisible(False)
        _hdr = self.etch_table.horizontalHeader()
        _hdr.setStretchLastSection(True)  # fill the width -> no horizontal scrollbar
        _hdr.setDefaultSectionSize(70)
        self.etch_table.setToolTip(
            "Partial-etch slab layers (e.g. SLAB150 on layer 2). 'Slab thickness' "
            "is the core height *remaining* after the etch (less than Core thickness "
            "above) — both FDTD and the mode solver then model a rib waveguide. "
            "Leave empty for a plain strip waveguide."
        )
        etch_layout.addWidget(self.etch_table)
        etch_buttons = QHBoxLayout()
        add_etch = QPushButton("Add etch layer")
        add_etch.clicked.connect(lambda: self._add_etch_row(2, 0, 0.0))
        remove_etch = QPushButton("Remove")
        remove_etch.clicked.connect(self._remove_etch_row)
        etch_buttons.addWidget(add_etch)
        etch_buttons.addWidget(remove_etch)
        etch_buttons.addStretch(1)
        etch_layout.addLayout(etch_buttons)
        form.addRow("Etch / slab layers", etch_widget)
        for e in initial.etch_layers:
            self._add_etch_row(e.layer, e.datatype, e.slab_thickness_um)

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

    def _add_etch_row(self, layer: int, datatype: int, slab_thickness_um: float) -> None:
        row = self.etch_table.rowCount()
        self.etch_table.insertRow(row)
        layer_spin = QSpinBox(); layer_spin.setRange(0, 255); layer_spin.setValue(int(layer))
        dt_spin = QSpinBox(); dt_spin.setRange(0, 255); dt_spin.setValue(int(datatype))
        slab_spin = QDoubleSpinBox()
        slab_spin.setRange(0.0, 100.0); slab_spin.setDecimals(3); slab_spin.setSuffix(" µm")
        slab_spin.setValue(float(slab_thickness_um))
        self.etch_table.setCellWidget(row, 0, layer_spin)
        self.etch_table.setCellWidget(row, 1, dt_spin)
        self.etch_table.setCellWidget(row, 2, slab_spin)

    def _remove_etch_row(self) -> None:
        row = self.etch_table.currentRow()
        if row < 0:
            row = self.etch_table.rowCount() - 1  # nothing selected -> drop the last
        if row >= 0:
            self.etch_table.removeRow(row)

    def _etch_layers(self) -> tuple[EtchLayer, ...]:
        out = []
        for row in range(self.etch_table.rowCount()):
            slab = self.etch_table.cellWidget(row, 2).value()
            if slab <= 0.0:
                continue  # a 0-thickness slab is a no-op; drop it rather than save it
            out.append(EtchLayer(
                layer=self.etch_table.cellWidget(row, 0).value(),
                datatype=self.etch_table.cellWidget(row, 1).value(),
                slab_thickness_um=slab,
            ))
        return tuple(out)

    def _on_clad_infinite_toggled(self, checked: bool) -> None:
        # The thickness value is ignored while infinite mode is on — grey it
        # out so that's visible rather than silently overridden.
        self.clad_thickness_spin.setEnabled(not checked)

    def result_settings(self) -> ProjectSettings:
        return ProjectSettings(
            platform_name=self.platform_combo.currentText(),
            core_index=self.core_index_spin.value(),
            clad_index=self.clad_index_spin.value(),
            thickness_um=self.thickness_spin.value(),
            clad_thickness_um=self.clad_thickness_spin.value(),
            clad_infinite=self.clad_infinite_check.isChecked(),
            wavelength_um=self.wavelength_spin.value(),
            cross_section=self.cross_section_combo.currentText(),
            etch_layers=self._etch_layers(),
        )
