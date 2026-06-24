from __future__ import annotations

import inspect
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._inst_id: int | None = None
        self._fields: dict[str, QWidget] = {}

        layout = QVBoxLayout(self)
        self.title_label = QLabel("No selection")
        layout.addWidget(self.title_label)

        self.form_layout = QFormLayout()
        layout.addLayout(self.form_layout)

        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self.apply_button)
        layout.addStretch(1)

        self.setEnabled(False)

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
