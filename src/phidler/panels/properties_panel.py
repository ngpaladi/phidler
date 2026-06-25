from __future__ import annotations

import inspect
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from phidler.pdk_catalog import list_cross_section_names

# Parameter names that take a value from a fixed PDK-defined vocabulary,
# mapped to the function that lists the valid options. Using a dropdown
# here prevents the invalid-edit path entirely (typo'd cross_section names
# used to reach gf.get_component and raise) instead of just catching it.
_VOCABULARY_FIELDS: dict[str, Callable[[], list[str]]] = {
    "cross_section": list_cross_section_names,
}


class PropertiesPanel(QWidget):
    """Dynamic parameter form for the selected instance, built from the
    component factory's signature. Non-scalar parameters (ComponentSpec,
    CrossSectionSpec callables, etc.) are shown read-only rather than
    guessed at — editing those is a stretch goal, not a v1 blocker."""

    params_applied = Signal(int, dict)
    # inst_id, x, y, rotation_deg, mirror, mag — precision/typed alternative
    # to dragging on the canvas, for exact placement (e.g. matching a known
    # coordinate from a foundry PDK or an existing layout) rather than
    # eyeballing a drag. Pushes the same MoveInstanceCommand drag/handle
    # gestures do, so it's undoable the same way.
    transform_applied = Signal(int, float, float, float, bool, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._inst_id: int | None = None
        self._fields: dict[str, QWidget] = {}

        layout = QVBoxLayout(self)
        self.title_label = QLabel("No selection")
        layout.addWidget(self.title_label)

        self.transform_group = QGroupBox("Transform")
        transform_layout = QFormLayout(self.transform_group)
        self.x_spin = self._make_transform_spin()
        self.y_spin = self._make_transform_spin()
        self.rotation_spin = self._make_transform_spin(minimum=-360.0, maximum=360.0, decimals=2)
        self.mirror_check = QCheckBox()
        self.scale_spin = self._make_transform_spin(minimum=0.001, maximum=1000.0, decimals=4)
        transform_layout.addRow("X (µm)", self.x_spin)
        transform_layout.addRow("Y (µm)", self.y_spin)
        transform_layout.addRow("Rotation (°)", self.rotation_spin)
        transform_layout.addRow("Mirror", self.mirror_check)
        transform_layout.addRow("Scale", self.scale_spin)
        self.apply_transform_button = QPushButton("Apply Transform")
        self.apply_transform_button.clicked.connect(self._on_apply_transform)
        transform_layout.addRow(self.apply_transform_button)
        layout.addWidget(self.transform_group)

        self.form_layout = QFormLayout()
        layout.addLayout(self.form_layout)

        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self.apply_button)
        layout.addStretch(1)

        self.setEnabled(False)

    @staticmethod
    def _make_transform_spin(minimum: float = -1e6, maximum: float = 1e6, decimals: int = 4) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        return spin

    def show_instance(
        self,
        inst_id: int,
        component_spec: str,
        signature: inspect.Signature,
        current_kwargs: dict[str, Any],
    ) -> None:
        self._inst_id = inst_id
        self._fields = {}
        self.title_label.setText(f"#{inst_id}: {component_spec}")
        self._clear_form()

        for p in signature.parameters.values():
            if p.default is inspect.Parameter.empty:
                continue
            value = current_kwargs.get(p.name, p.default)
            widget = self._make_field(p.name, value)
            self.form_layout.addRow(p.name, widget)
            self._fields[p.name] = widget

        self.setEnabled(True)

    def update_transform(self, x: float, y: float, rotation: float, mirror: bool, mag: float) -> None:
        """Syncs the transform fields to the instance's current values —
        called on selection change and from the same periodic timer that
        keeps the on-canvas transform handles positioned, so a drag/handle
        gesture is reflected here too. Skips the sync entirely while the
        user has focus in one of these fields, the same is-interacting
        guard the handles use for their own periodic resync, so typing
        isn't clobbered mid-edit."""
        if self._is_editing_transform():
            return
        self.x_spin.blockSignals(True)
        self.y_spin.blockSignals(True)
        self.rotation_spin.blockSignals(True)
        self.mirror_check.blockSignals(True)
        self.scale_spin.blockSignals(True)
        self.x_spin.setValue(x)
        self.y_spin.setValue(y)
        self.rotation_spin.setValue(rotation)
        self.mirror_check.setChecked(mirror)
        self.scale_spin.setValue(mag)
        self.x_spin.blockSignals(False)
        self.y_spin.blockSignals(False)
        self.rotation_spin.blockSignals(False)
        self.mirror_check.blockSignals(False)
        self.scale_spin.blockSignals(False)

    def _is_editing_transform(self) -> bool:
        return any(
            w.hasFocus() for w in (self.x_spin, self.y_spin, self.rotation_spin, self.mirror_check, self.scale_spin)
        )

    def _on_apply_transform(self) -> None:
        if self._inst_id is None:
            return
        self.transform_applied.emit(
            self._inst_id,
            self.x_spin.value(),
            self.y_spin.value(),
            self.rotation_spin.value(),
            self.mirror_check.isChecked(),
            self.scale_spin.value(),
        )

    def clear(self) -> None:
        self._inst_id = None
        self._fields = {}
        self.title_label.setText("No selection")
        self._clear_form()
        self.setEnabled(False)

    def _clear_form(self) -> None:
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)

    @staticmethod
    def _make_field(name: str, value: Any) -> QWidget:
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
            return w
        if isinstance(value, int):
            w = QSpinBox()
            w.setRange(-1_000_000, 1_000_000)
            w.setValue(value)
            return w
        if isinstance(value, float):
            w = QDoubleSpinBox()
            w.setRange(-1e6, 1e6)
            w.setDecimals(4)
            w.setValue(value)
            return w
        if isinstance(value, str):
            list_options = _VOCABULARY_FIELDS.get(name)
            if list_options is not None:
                w = QComboBox()
                options = list_options()
                w.addItems(options)
                if value in options:
                    w.setCurrentText(value)
                return w
            return QLineEdit(value)
        w = QLineEdit(repr(value))
        w.setEnabled(False)
        w.setToolTip("This parameter type isn't editable from the panel yet.")
        return w

    def _on_apply(self) -> None:
        if self._inst_id is None:
            return
        kwargs: dict[str, Any] = {}
        for name, w in self._fields.items():
            if isinstance(w, QCheckBox):
                kwargs[name] = w.isChecked()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                kwargs[name] = w.value()
            elif isinstance(w, QComboBox):
                kwargs[name] = w.currentText()
            elif isinstance(w, QLineEdit) and w.isEnabled():
                kwargs[name] = w.text()
        self.params_applied.emit(self._inst_id, kwargs)
