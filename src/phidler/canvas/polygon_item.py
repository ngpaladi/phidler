from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from phidler.model.layers import LayerInfo, LayerKey

_SELECTION_PEN = QPen(QColor("#ffffff"), 0, Qt.DashLine)
_PORT_PEN = QPen(QColor("#ffd400"), 0)
_PORT_BRUSH = QBrush(QColor("#ffd400"))
_PORT_MARKER_RADIUS = 0.15  # microns, fallback visual size when port width is tiny
_PORT_HIT_RADIUS = 0.6  # microns, "close enough" tolerance for routing-mode clicks
_REFERENCE_COLOR = QColor(140, 140, 140, 90)  # dim translucent gray for backdrop GDS


def _shape_to_path(hull: list[tuple[float, float]], holes: list[list[tuple[float, float]]]) -> QPainterPath:
    """Builds hull-with-holes as one odd-even-fill path so a hole renders as
    a real cut-out rather than being silently dropped (gdsfactory geometry
    can carry true polygon holes even though GDS export keyhole-resolves
    them — the canvas must still show the cut-out to stay WYSIWYG)."""
    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)
    path.addPolygon(_to_qpolygon(hull))
    path.closeSubpath()
    for hole in holes:
        path.addPolygon(_to_qpolygon(hole))
        path.closeSubpath()
    return path


def _to_qpolygon(pts: list[tuple[float, float]]) -> QPolygonF:
    return QPolygonF([QPointF(x, y) for x, y in pts])


class InstanceItem(QGraphicsItem):
    """Renders one PlacedInstance as a group of per-layer shape children.

    Geometry (the polygon/hole point lists) is built once from gdsfactory on
    place/edit and cached; interactive move/rotate only touches this item's
    Qt transform/pos, never gdsfactory, so dragging stays responsive
    regardless of layout complexity.

    Children are plain QGraphicsPathItems parented (not grouped) onto this
    item with setHandlesChildEvents(True), so a click anywhere on the
    instance's geometry selects/drags this item as a whole rather than an
    individual shape.
    """

    def __init__(
        self,
        inst_id: int,
        parent: QGraphicsItem | None = None,
        movable: bool = True,
        selectable: bool = True,
    ) -> None:
        super().__init__(parent)
        self.inst_id = inst_id
        self.is_route = False
        self.is_reference = False
        if selectable:
            self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        if movable:
            self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setHandlesChildEvents(True)
        self._layer_children: dict[LayerKey, list[QGraphicsPathItem]] = {}
        self._bounds = QRectF()
        self.rotation_deg = 0.0
        self.mirror = False
        self.mag = 1.0
        self._ports: list[tuple[str, float, float]] = []  # (name, x, y) in local coords

    def boundingRect(self) -> QRectF:
        return self._bounds

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            self.scene().mark_dirty(self.inst_id)
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None) -> None:
        if self.isSelected():
            painter.setPen(_SELECTION_PEN)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self._bounds)
        if self._ports:
            painter.setPen(_PORT_PEN)
            painter.setBrush(_PORT_BRUSH)
            for _name, x, y in self._ports:
                painter.drawEllipse(QPointF(x, y), _PORT_MARKER_RADIUS, _PORT_MARKER_RADIUS)

    def set_ports(self, ports: list[tuple[str, float, float, float, float]]) -> None:
        """ports: (name, x, y, orientation_deg, width) in local coordinates."""
        self._ports = [(name, x, y) for name, x, y, _orientation, _width in ports]

    def nearest_port(self, local_pt: QPointF) -> str | None:
        if not self._ports:
            return None
        best_name, best_dist2 = None, None
        for name, x, y in self._ports:
            dist2 = (local_pt.x() - x) ** 2 + (local_pt.y() - y) ** 2
            if best_dist2 is None or dist2 < best_dist2:
                best_name, best_dist2 = name, dist2
        if best_dist2 is not None and best_dist2 <= _PORT_HIT_RADIUS**2:
            return best_name
        return None

    def mousePressEvent(self, event) -> None:
        scene = self.scene()
        if scene is not None and getattr(scene, "routing_mode", False) and event.button() == Qt.LeftButton:
            port_name = self.nearest_port(event.pos())
            if port_name is not None:
                scene.port_clicked.emit(self.inst_id, port_name)
                event.accept()
                return
        super().mousePressEvent(event)

    def set_geometry(
        self,
        shapes_by_layer: dict[LayerKey, list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]],
        layers: dict[LayerKey, LayerInfo],
    ) -> None:
        for child in list(self.childItems()):
            child.setParentItem(None)
            if child.scene() is not None:
                child.scene().removeItem(child)
        self._layer_children.clear()

        bounds = QRectF()
        for key, shapes in shapes_by_layer.items():
            info = layers.get(key)
            if self.is_reference:
                color = _REFERENCE_COLOR
                visible = True
            else:
                color = QColor(info.color) if info else QColor("#888888")
                visible = info.visible if info else True
            for hull, holes in shapes:
                path = _shape_to_path(hull, holes)
                item = QGraphicsPathItem(path, self)
                item.setBrush(QBrush(color))
                item.setPen(QPen(color.darker(120), 0))
                item.setVisible(visible)
                self._layer_children.setdefault(key, []).append(item)
                bounds = bounds.united(path.boundingRect())
        self.prepareGeometryChange()
        self._bounds = bounds

    def set_layer_visible(self, key: LayerKey, visible: bool) -> None:
        for item in self._layer_children.get(key, []):
            item.setVisible(visible)

    def set_layer_color(self, key: LayerKey, color: str) -> None:
        if self.is_reference:
            return  # backdrop always renders in its fixed dim color
        qcolor = QColor(color)
        for item in self._layer_children.get(key, []):
            item.setBrush(QBrush(qcolor))
            item.setPen(QPen(qcolor.darker(120), 0))

    def apply_transform(self, x: float, y: float, rotation: float, mirror: bool, mag: float = 1.0) -> None:
        """Reproduces klayout's DCplxTrans(mag, rotation, mirror, x, y):
        mirror-about-x-axis, then uniform scale by mag, then rotate, then
        translate. Verified empirically against klayout's DCplxTrans.trans()
        directly — both the rotate-after-mirror call order (QTransform
        applies the most-recently-called op first to a point) and that
        uniform scale commutes with an axis mirror, so a single combined
        scale(mag, -mag if mirror else mag) call reproduces mirror-then-mag
        in one step without needing two separate scale() calls."""
        t = QTransform()
        t.rotate(rotation)
        t.scale(mag, -mag if mirror else mag)
        self.setTransform(t)
        self.setPos(x, y)
