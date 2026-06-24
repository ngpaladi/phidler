from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QPushButton,
    QSlider,
    QToolButton,
    QWidget,
)

_ROTATION_RANGE = (0, 359)
_SCALE_PERCENT_RANGE = (10, 400)  # 10%-400%, i.e. mag 0.1-4.0


class TransformOverlay(QWidget):
    """Floating buttons/sliders for rotate/mirror/scale, shown directly
    over the canvas near the selected instance — the on-canvas alternative
    to the keyboard shortcuts and the Properties panel.

    Deliberately "dumb" like the other panels in this app: it only emits
    signals with the values the user picked and exposes set_values() to
    sync its display — all model/undo-stack logic lives in MainWindow.
    """

    rotate_by_requested = Signal(float)  # relative degrees, e.g. +90/-90
    rotation_set_live = Signal(float)  # absolute degrees, while dragging (visual only)
    rotation_committed = Signal(float)  # absolute degrees, on release (undoable)
    mirror_toggle_requested = Signal()
    scale_set_live = Signal(float)  # absolute mag, while dragging (visual only)
    scale_committed = Signal(float)  # absolute mag, on release (undoable)
    reset_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "QWidget#TransformOverlay {"
            "  background-color: rgba(30, 30, 30, 230);"
            "  border: 1px solid #555;"
            "  border-radius: 6px;"
            "}"
            "QLabel { color: #ddd; }"
        )
        self.setObjectName("TransformOverlay")

        grid = QGridLayout(self)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(6)

        self.rotate_ccw_button = QToolButton()
        self.rotate_ccw_button.setText("⟲")
        self.rotate_ccw_button.setToolTip("Rotate -90°")
        self.rotate_ccw_button.clicked.connect(lambda: self.rotate_by_requested.emit(-90.0))
        grid.addWidget(self.rotate_ccw_button, 0, 0)

        self.rotation_slider = QSlider(Qt.Horizontal)
        self.rotation_slider.setRange(*_ROTATION_RANGE)
        self.rotation_slider.setToolTip("Free rotation")
        self.rotation_slider.valueChanged.connect(self._on_rotation_value_changed)
        self.rotation_slider.sliderReleased.connect(
            lambda: self.rotation_committed.emit(float(self.rotation_slider.value()))
        )
        grid.addWidget(self.rotation_slider, 0, 1)

        self.rotate_cw_button = QToolButton()
        self.rotate_cw_button.setText("⟳")
        self.rotate_cw_button.setToolTip("Rotate +90°")
        self.rotate_cw_button.clicked.connect(lambda: self.rotate_by_requested.emit(90.0))
        grid.addWidget(self.rotate_cw_button, 0, 2)

        self.rotation_label = QLabel("0°")
        self.rotation_label.setFixedWidth(36)
        grid.addWidget(self.rotation_label, 0, 3)

        self.mirror_button = QToolButton()
        self.mirror_button.setText("Mirror")
        self.mirror_button.setCheckable(True)
        self.mirror_button.setToolTip("Mirror about the instance's local x-axis")
        self.mirror_button.clicked.connect(self.mirror_toggle_requested.emit)
        grid.addWidget(self.mirror_button, 1, 0)

        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(*_SCALE_PERCENT_RANGE)
        self.scale_slider.setToolTip("Geometric scale of the placed instance (not a parameter like length)")
        self.scale_slider.valueChanged.connect(self._on_scale_value_changed)
        self.scale_slider.sliderReleased.connect(
            lambda: self.scale_committed.emit(self.scale_slider.value() / 100.0)
        )
        grid.addWidget(self.scale_slider, 1, 1)

        self.scale_label = QLabel("100%")
        self.scale_label.setFixedWidth(36)
        grid.addWidget(self.scale_label, 1, 3)

        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Reset rotation to 0°, clear mirror, reset scale to 100%")
        self.reset_button.clicked.connect(self.reset_requested.emit)
        grid.addWidget(self.reset_button, 2, 0, 1, 4)

    def is_interacting(self) -> bool:
        """True while the user is mid-drag on a slider — callers should
        skip calling set_values() during this window, or the periodic
        position/value sync would fight the user's own drag."""
        return self.rotation_slider.isSliderDown() or self.scale_slider.isSliderDown()

    def _on_rotation_value_changed(self, value: int) -> None:
        # Label update is independent of the live-preview signal: it must
        # track the slider while the user drags it, which is exactly the
        # window set_values() is told to stay out of (see is_interacting()).
        self.rotation_label.setText(f"{value}°")
        self.rotation_set_live.emit(float(value))

    def _on_scale_value_changed(self, value: int) -> None:
        self.scale_label.setText(f"{value}%")
        self.scale_set_live.emit(value / 100.0)

    def set_values(self, rotation: float, mirror: bool, mag: float) -> None:
        rotation_int = round(rotation) % 360
        self.rotation_slider.blockSignals(True)
        self.rotation_slider.setValue(rotation_int)
        self.rotation_slider.blockSignals(False)
        self.rotation_label.setText(f"{rotation_int}°")

        self.mirror_button.blockSignals(True)
        self.mirror_button.setChecked(mirror)
        self.mirror_button.blockSignals(False)

        percent = max(_SCALE_PERCENT_RANGE[0], min(_SCALE_PERCENT_RANGE[1], round(mag * 100)))
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(percent)
        self.scale_slider.blockSignals(False)
        self.scale_label.setText(f"{percent}%")
