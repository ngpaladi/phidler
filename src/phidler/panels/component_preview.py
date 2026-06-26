from __future__ import annotations

import gdsfactory as gf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from phidler.canvas.polygon_item import _shape_to_path
from phidler.model.document import shapes_for_cell
from phidler.model.layers import _color_for

_PIXMAP_SIZE = (180, 140)
_PADDING_PX = 14
_BACKGROUND = QColor("#1e1e1e")

_cache: dict[str, QPixmap | None] = {}


def render_component_pixmap(name: str) -> QPixmap | None:
    """Renders a component's actual geometry (not an icon/placeholder) at
    its default kwargs, scaled to fit a small fixed-size pixmap. Cached by
    name since the same item gets hovered repeatedly and re-extracting
    polygons from gdsfactory on every mouse-move would be wasteful."""
    if name not in _cache:
        _cache[name] = _build_pixmap(name)
    return _cache[name]


def _build_pixmap(name: str) -> QPixmap | None:
    try:
        cell = gf.get_component(name)
    except Exception:
        return None

    shapes_by_layer = shapes_for_cell(cell)
    all_hulls = [hull for shapes in shapes_by_layer.values() for hull, _holes in shapes]
    if not all_hulls:
        return None

    xs = [x for hull in all_hulls for x, _y in hull]
    ys = [y for hull in all_hulls for _x, y in hull]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    width_um = max(xmax - xmin, 1e-6)
    height_um = max(ymax - ymin, 1e-6)

    pixel_w, pixel_h = _PIXMAP_SIZE
    avail_w, avail_h = pixel_w - 2 * _PADDING_PX, pixel_h - 2 * _PADDING_PX
    scale = min(avail_w / width_um, avail_h / height_um)
    offset_x = _PADDING_PX + (avail_w - width_um * scale) / 2
    offset_y = _PADDING_PX + (avail_h - height_um * scale) / 2

    def to_pixel(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        # GDS is Y-up; pixmap pixels are Y-down, so this flip is the static
        # raster equivalent of the canvas's single global Y-flip transform.
        return (offset_x + (x - xmin) * scale, offset_y + (ymax - y) * scale)

    pixmap = QPixmap(pixel_w, pixel_h)
    pixmap.fill(_BACKGROUND)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    for layer_key, shapes in sorted(shapes_by_layer.items()):
        color = QColor(_color_for(*layer_key))
        fill = QColor(color)
        fill.setAlpha(180)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(color.darker(120), 0))
        for hull, holes in shapes:
            mapped_hull = [to_pixel(p) for p in hull]
            mapped_holes = [[to_pixel(p) for p in hole] for hole in holes]
            painter.drawPath(_shape_to_path(mapped_hull, mapped_holes))

    painter.end()
    return pixmap


class ComponentPreviewPopup(QWidget):
    """Floating, tooltip-styled preview of a component's actual rendered
    geometry, shown near the cursor while hovering a palette leaf item."""

    def __init__(self) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self._label = QLabel()
        layout.addWidget(self._label)

    def show_for(self, name: str, global_pos: QPoint) -> None:
        pixmap = render_component_pixmap(name)
        if pixmap is None:
            self.hide()
            return
        self._label.setPixmap(pixmap)
        self.adjustSize()
        self.move(global_pos)
        self.show()
