"""On-canvas graphics for one :class:`~phidler.model.annotation.Annotation`.

Renders the note as a small pin at its location with a constant-size text label,
plus its callout drawings (a rectangle or an arrow) tied back to the pin by a
dashed leader line — everything in the note's colour. The pin, label and leader
make it read as "this note is about *that*".

Coordinate notes: the item is placed at the pin (setPos(x, y)); shape points are
relative to the pin, so they paint directly in the item's local frame and move
with it. The text label is a child with ``ItemIgnoresTransformations`` so it
stays upright (despite the view's global Y-flip) and a constant on-screen size at
any zoom — the same trick the measurement readout and transform handles use.

Move is self-handled (a plain QGraphicsItem has no signals): a drag pushes a
MoveAnnotationCommand onto the view's undo stack on release, mirroring how the
transform handles talk directly to the model.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSimpleTextItem

_PIN_RADIUS_UM = 0.8      # note pin marker (scene µm — scales with the layout)
_ARROWHEAD_UM = 2.5       # callout arrowhead leg length (scene µm)
_ARROWHEAD_ANGLE = math.radians(26.0)
_BOUNDS_MARGIN_UM = 1.0
_Z_VALUE = 2000.0         # above layout geometry, alongside the other markers


class AnnotationItem(QGraphicsItem):
    """Selectable, movable graphics for one note and its callouts. Rebuilt from
    the document via :meth:`sync_from_document` on create/edit/move."""

    def __init__(self, ann_id: int) -> None:
        super().__init__()
        self.ann_id = ann_id
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setHandlesChildEvents(True)  # clicks on the label select/drag the note
        self.setZValue(_Z_VALUE)

        self._color = QColor("#f4b400")
        self._shapes: list[tuple[str, list[tuple[float, float]]]] = []
        self._bounds = QRectF()

        # Constant-size, upright text label anchored at the pin.
        self._text_item = QGraphicsSimpleTextItem("", self)
        self._text_item.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self._text_item.setPos(QPointF(_PIN_RADIUS_UM, _PIN_RADIUS_UM))

        self._drag_origin: tuple[float, float] | None = None

    # -- model sync ---------------------------------------------------------

    def sync_from_document(self, document) -> None:
        ann = document.annotations.get(self.ann_id)
        if ann is None:
            return
        self.setPos(ann.x, ann.y)
        self._color = QColor(ann.color)
        self._shapes = [(s.kind, [tuple(p) for p in s.points]) for s in ann.shapes]
        self._text_item.setText(ann.text)
        self._text_item.setBrush(QBrush(self._color))
        self.prepareGeometryChange()
        self._bounds = self._compute_bounds()
        self.update()

    def _compute_bounds(self) -> QRectF:
        bounds = QRectF(-_PIN_RADIUS_UM, -_PIN_RADIUS_UM, 2 * _PIN_RADIUS_UM, 2 * _PIN_RADIUS_UM)
        for _kind, points in self._shapes:
            for x, y in points:
                bounds = bounds.united(QRectF(x, y, 0, 0))
        m = _ARROWHEAD_UM + _BOUNDS_MARGIN_UM  # cover arrowheads drawn past the head point
        return bounds.adjusted(-m, -m, m, m)

    def boundingRect(self) -> QRectF:
        return self._bounds

    # -- painting -----------------------------------------------------------

    def paint(self, painter, option, widget=None) -> None:
        outline = QPen(self._color, 2)
        outline.setCosmetic(True)  # constant 2px stroke regardless of zoom
        leader = QPen(self._color, 1, Qt.DashLine)
        leader.setCosmetic(True)

        for kind, points in self._shapes:
            if len(points) < 2:
                continue
            (x0, y0), (x1, y1) = points[0], points[1]
            anchor = self._draw_shape(painter, outline, kind, x0, y0, x1, y1)
            # Leader from the pin (local origin) to the shape, so the note owns it.
            painter.setPen(leader)
            painter.drawLine(QPointF(0.0, 0.0), anchor)

        # Pin marker at the note's location.
        painter.setPen(QPen(self._color.darker(140), 1) if not self.isSelected() else QPen(QColor("#ffffff"), 2))
        painter.setBrush(QBrush(self._color))
        painter.drawEllipse(QPointF(0.0, 0.0), _PIN_RADIUS_UM, _PIN_RADIUS_UM)

        if self.isSelected():
            sel = QPen(QColor("#ffffff"), 0, Qt.DashLine)
            painter.setPen(sel)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self._bounds)

    def _draw_shape(self, painter, pen, kind, x0, y0, x1, y1) -> QPointF:
        """Draw one callout and return the point a leader line should join."""
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if kind == "rect":
            painter.drawRect(QRectF(QPointF(x0, y0), QPointF(x1, y1)))
            return QRectF(QPointF(x0, y0), QPointF(x1, y1)).center()
        # "arrow": a line from tail (x0,y0) to head (x1,y1) with a wedge head.
        tail, head = QPointF(x0, y0), QPointF(x1, y1)
        painter.drawLine(tail, head)
        ang = math.atan2(y1 - y0, x1 - x0)
        for sign in (+1, -1):
            a = ang + math.pi - sign * _ARROWHEAD_ANGLE
            painter.drawLine(head, QPointF(x1 + _ARROWHEAD_UM * math.cos(a), y1 + _ARROWHEAD_UM * math.sin(a)))
        return tail

    # -- self-handled move --------------------------------------------------

    def mousePressEvent(self, event) -> None:
        self._drag_origin = (self.pos().x(), self.pos().y())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        scene = self.scene()
        if self._drag_origin is None or scene is None:
            return
        old_x, old_y = self._drag_origin
        self._drag_origin = None
        new_x, new_y = self.pos().x(), self.pos().y()

        view = scene.views()[0] if scene.views() else None
        if view is not None and getattr(view, "snap_enabled", False):
            new_x, new_y = view.snap(new_x), view.snap(new_y)
        if (old_x, old_y) == (new_x, new_y):
            return

        undo = getattr(view, "undo_stack", None) if view is not None else None
        if undo is not None:
            from phidler.model.commands import MoveAnnotationCommand

            undo.push(MoveAnnotationCommand(scene.document, scene, self.ann_id, old_x, old_y, new_x, new_y))
        else:  # no undo stack wired (e.g. a bare scene in a test) — apply directly
            scene.document.set_annotation_position(self.ann_id, new_x, new_y)
            self.sync_from_document(scene.document)
