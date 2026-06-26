from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from phidler.model.layers import LayerInfo, LayerKey, layer_description


class _LayerRow(QWidget):
    def __init__(self, info: LayerInfo, parent=None) -> None:
        super().__init__(parent)
        self.key = info.key
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        self.visible_box = QCheckBox()
        self.visible_box.setChecked(info.visible)
        layout.addWidget(self.visible_box)

        self.color_button = QPushButton()
        self.color_button.setFixedSize(18, 18)
        self._set_swatch_color(info.color)
        layout.addWidget(self.color_button)

        label = QLabel(f"{info.name}  ({info.layer}/{info.datatype})")
        desc = layer_description(info.name)
        if desc:
            label.setToolTip(desc)
            self.setToolTip(desc)
        layout.addWidget(label)
        layout.addStretch(1)

    def _set_swatch_color(self, color: str) -> None:
        self.color_button.setStyleSheet(f"background-color: {color}; border: 1px solid #555;")


class LayersPanel(QWidget):
    """Lists the document's layers with visibility toggles and color swatches."""

    visibility_changed = Signal(tuple, bool)
    color_changed = Signal(tuple, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Layers"))
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

    def refresh(self, layers: dict[LayerKey, LayerInfo]) -> None:
        self.list_widget.clear()
        for info in sorted(layers.values(), key=lambda li: (li.layer, li.datatype)):
            row = _LayerRow(info)
            row.visible_box.toggled.connect(lambda checked, key=info.key: self.visibility_changed.emit(key, checked))
            row.color_button.clicked.connect(lambda _checked=False, row=row: self._pick_color(row))
            item = QListWidgetItem()
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, row)

    def _pick_color(self, row: _LayerRow) -> None:
        initial = QColor(row.color_button.palette().button().color())
        color = QColorDialog.getColor(initial, self, "Layer color")
        if color.isValid():
            hex_color = color.name()
            row._set_swatch_color(hex_color)
            self.color_changed.emit(row.key, hex_color)
