from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from phidler.drc import DrcViolation
from phidler.model.layers import LayerInfo, LayerKey


class DrcPanel(QWidget):
    """Runs width/spacing checks against thresholds the user enters here —
    not against any official foundry rule deck (the generic PDK doesn't
    expose one). The disclaimer label is load-bearing, not decoration."""

    run_requested = Signal(tuple, float, float)  # layer_key, min_width, min_spacing
    violation_selected = Signal(float, float, float, float)  # left, bottom, right, top

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        disclaimer = QLabel(
            "Checks against the thresholds entered below only — results are "
            "not validated against any official foundry design rules."
        )
        disclaimer.setWordWrap(True)
        layout.addWidget(disclaimer)

        form = QFormLayout()
        self.layer_combo = QComboBox()
        self.layer_combo.setToolTip(
            "The single layer the width/spacing check runs against. Only "
            "geometry on this layer is examined; run the check once per layer "
            "you want to verify."
        )
        form.addRow("Layer", self.layer_combo)

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setDecimals(3)
        self.width_spin.setRange(0.0, 1000.0)
        self.width_spin.setValue(0.2)
        self.width_spin.setToolTip(
            "Minimum feature width in microns. Shapes on the selected layer "
            "narrower than this are flagged as width violations. This is your "
            "own threshold, not a foundry rule."
        )
        form.addRow("Min width (µm)", self.width_spin)

        self.spacing_spin = QDoubleSpinBox()
        self.spacing_spin.setDecimals(3)
        self.spacing_spin.setRange(0.0, 1000.0)
        self.spacing_spin.setValue(0.2)
        self.spacing_spin.setToolTip(
            "Minimum gap in microns between separate shapes on the selected "
            "layer. Shapes closer than this are flagged as spacing violations. "
            "This is your own threshold, not a foundry rule."
        )
        form.addRow("Min spacing (µm)", self.spacing_spin)

        layout.addLayout(form)

        self.run_button = QPushButton("Run Check")
        self.run_button.setToolTip(
            "Check the selected layer against the width and spacing thresholds "
            "above and list any violations below. Does nothing if no layer is "
            "selected."
        )
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)

        self.results_list = QListWidget()
        self.results_list.setToolTip(
            "Violations found by the last check. Double-click an entry to "
            "center the layout view on that region."
        )
        self.results_list.itemDoubleClicked.connect(self._on_result_double_clicked)
        layout.addWidget(self.results_list)

        self._violations: list[DrcViolation] = []

    def set_layers(self, layers: dict[LayerKey, LayerInfo]) -> None:
        current = self.layer_combo.currentData()
        self.layer_combo.clear()
        for info in sorted(layers.values(), key=lambda li: (li.layer, li.datatype)):
            self.layer_combo.addItem(f"{info.name} ({info.layer}/{info.datatype})", info.key)
        if current is not None:
            idx = self.index_for_layer(current)
            if idx >= 0:
                self.layer_combo.setCurrentIndex(idx)

    def index_for_layer(self, key: LayerKey) -> int:
        """QComboBox.findData() compares stored Python tuples unreliably
        (it doesn't consistently use Python equality for opaque itemData),
        so this looks the layer key up by iterating and comparing in
        Python directly instead."""
        for i in range(self.layer_combo.count()):
            if self.layer_combo.itemData(i) == key:
                return i
        return -1

    def set_current_layer(self, key: LayerKey) -> bool:
        idx = self.index_for_layer(key)
        if idx >= 0:
            self.layer_combo.setCurrentIndex(idx)
        return idx >= 0

    def _on_run_clicked(self) -> None:
        layer_key = self.layer_combo.currentData()
        if layer_key is None:
            return
        self.run_requested.emit(layer_key, self.width_spin.value(), self.spacing_spin.value())

    def show_results(self, violations: list[DrcViolation]) -> None:
        self._violations = violations
        self.results_list.clear()
        if not violations:
            self.results_list.addItem("No violations against the entered thresholds.")
            return
        for v in violations:
            self.results_list.addItem(f"{v.kind} violation near ({v.bbox[0]:.3f}, {v.bbox[1]:.3f})")

    def _on_result_double_clicked(self, item: QListWidgetItem) -> None:
        index = self.results_list.row(item)
        if 0 <= index < len(self._violations):
            v = self._violations[index]
            self.violation_selected.emit(*v.bbox)
