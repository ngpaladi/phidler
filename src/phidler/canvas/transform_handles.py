from __future__ import annotations

import math

import klayout.db as kdb
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem

from phidler.model.document import LayoutDocument, Transform

_HANDLE_PIXELS = 9.0  # constant on-screen size regardless of zoom
_HANDLE_FILL = QColor("#ffd400")
_HANDLE_PEN = QPen(QColor("#000000"), 1)
_MIN_MAG = 0.05
_MAX_MAG = 50.0

# Corner indices into [(xmin,ymin), (xmax,ymin), (xmax,ymax), (xmin,ymax)];
# CORNER_OPPOSITE[i] is the diagonal partner of corner i.
_CORNER_OPPOSITE = {0: 2, 1: 3, 2: 0, 3: 1}


def _local_corners(rect: QRectF) -> list[QPointF]:
    return [
        QPointF(rect.left(), rect.top()),
        QPointF(rect.right(), rect.top()),
        QPointF(rect.right(), rect.bottom()),
        QPointF(rect.left(), rect.bottom()),
    ]


class _BaseHandle(QGraphicsItem):
    """Shared plumbing for the on-canvas transform handles: constant
    screen-pixel size regardless of zoom (ItemIgnoresTransformations,
    verified empirically to keep the local bounding rect's device-pixel
    footprint fixed across view scale changes), drawn above everything
    else, and self-contained drag handling that talks directly to
    (document, scene, undo_stack) — the same pattern LayoutView itself
    already uses for drag-to-move, rather than the signal-emitting "dumb
    widget" pattern the old QWidget-based TransformOverlay used. A plain
    QGraphicsItem has no QObject/signal support anyway, so directly poking
    the model here is the natural fit, not a shortcut."""

    def __init__(self, document: LayoutDocument, layout_scene, undo_stack, on_drag_update) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setZValue(1000.0)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.document = document
        self.layout_scene = layout_scene
        self.undo_stack = undo_stack
        self._on_drag_update = on_drag_update  # callback: reposition all handles mid-drag
        self.inst_id: int | None = None
        self.is_dragging = False
        self._old_transform: Transform | None = None

    def boundingRect(self) -> QRectF:
        half = _HANDLE_PIXELS / 2
        return QRectF(-half, -half, _HANDLE_PIXELS, _HANDLE_PIXELS)

    def _target_item(self):
        if self.inst_id is None:
            return None
        return self.layout_scene.items_by_inst.get(self.inst_id)

    def mouseReleaseEvent(self, event) -> None:
        if not self.is_dragging:
            return
        self.is_dragging = False
        item = self._target_item()
        old_t = self._old_transform
        if item is None or old_t is None:
            return
        from phidler.model.commands import MoveInstanceCommand

        new_t = Transform(x=item.pos().x(), y=item.pos().y(), rotation=item.rotation_deg, mirror=item.mirror, mag=item.mag)
        if (old_t.x, old_t.y, old_t.rotation, old_t.mirror, old_t.mag) != (new_t.x, new_t.y, new_t.rotation, new_t.mirror, new_t.mag):
            self.undo_stack.push(MoveInstanceCommand(self.document, self.layout_scene, self.inst_id, old_t, new_t))
        event.accept()

    def paint(self, painter, option, widget=None) -> None:
        painter.setBrush(QBrush(_HANDLE_FILL))
        painter.setPen(_HANDLE_PEN)
        self._paint_shape(painter)

    def _paint_shape(self, painter) -> None:
        raise NotImplementedError


class ScaleHandleItem(_BaseHandle):
    """One of 4 corner handles. Dragging scales the instance uniformly
    (the document model only supports uniform `mag`, never independent
    x/y scale) while keeping the *diagonally opposite* corner fixed in
    absolute scene coordinates — the standard resize-handle convention.

    Anchoring the scale at the instance's own local origin (where `mag`
    actually pivots, per klayout's DCplxTrans) would NOT give that
    behavior on its own: for a component like `straight`, whose local
    origin sits almost on top of one edge of its bounding box, that
    corner would barely move while the far corner swings wildly for a
    tiny mouse movement. So a corner drag here updates BOTH `mag` and
    `(x, y)` together — solved once at drag-start (see mousePressEvent)
    and reused every move, rather than re-derived per frame."""

    def __init__(self, corner_index: int, document: LayoutDocument, layout_scene, undo_stack, on_drag_update) -> None:
        super().__init__(document, layout_scene, undo_stack, on_drag_update)
        self.corner_index = corner_index
        self.setCursor(Qt.SizeFDiagCursor if corner_index in (0, 2) else Qt.SizeBDiagCursor)
        self._opp_scene: QPointF | None = None  # fixed absolute anchor for this drag
        self._q_opp: QPointF | None = None  # rotation+mirror-only image of the opposite corner
        self._v: QPointF | None = None  # fixed diagonal direction vector for this drag
        self._dot_vv = 0.0

    def _paint_shape(self, painter) -> None:
        painter.drawRect(self.boundingRect())

    def mousePressEvent(self, event) -> None:
        item = self._target_item()
        if item is None:
            return
        old_t = self.document.get_transform(self.inst_id)
        self._old_transform = old_t

        corners = _local_corners(item.boundingRect())
        p_drag_local = corners[self.corner_index]
        p_opp_local = corners[_CORNER_OPPOSITE[self.corner_index]]

        # q(p) = the rotation+mirror-only image of a local point (mag=1,
        # untranslated) — mag-independent and translation-independent, so
        # this single helper transform is reusable for both corners.
        q = kdb.DCplxTrans(1.0, old_t.rotation, old_t.mirror, 0.0, 0.0)
        q_opp = q.trans(kdb.DPoint(p_opp_local.x(), p_opp_local.y()))
        q_drag = q.trans(kdb.DPoint(p_drag_local.x(), p_drag_local.y()))

        self._opp_scene = QPointF(old_t.x + old_t.mag * q_opp.x, old_t.y + old_t.mag * q_opp.y)
        self._q_opp = QPointF(q_opp.x, q_opp.y)
        self._v = QPointF(q_drag.x - q_opp.x, q_drag.y - q_opp.y)
        self._dot_vv = self._v.x() ** 2 + self._v.y() ** 2

        self.is_dragging = True
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self.is_dragging:
            return
        item = self._target_item()
        if item is None or self._dot_vv <= 1e-12:
            return
        mouse = event.scenePos()
        rel = QPointF(mouse.x() - self._opp_scene.x(), mouse.y() - self._opp_scene.y())
        t = (rel.x() * self._v.x() + rel.y() * self._v.y()) / self._dot_vv
        new_mag = max(_MIN_MAG, min(_MAX_MAG, t))
        new_x = self._opp_scene.x() - new_mag * self._q_opp.x()
        new_y = self._opp_scene.y() - new_mag * self._q_opp.y()

        item.apply_transform(new_x, new_y, item.rotation_deg, item.mirror, new_mag)
        item.mag = new_mag
        self._on_drag_update()
        event.accept()


class RotateHandleItem(_BaseHandle):
    """A single handle offset above the instance's bounding box (in its
    own local frame, so it rotates along with the shape between drags).
    Dragging it rotates the instance around its local origin — the same
    pivot the keyboard shortcut (R) and context-menu rotate already use,
    so unlike scale, no position compensation is needed here.

    Uses a delta-angle approach (how far the mouse has swept around the
    pivot since the press, added to the rotation at press-time) rather
    than computing an absolute target angle — verified empirically that
    the scene-frame atan2 angle and the `rotation` parameter move in the
    same direction by the same amount, so this delta is correct without
    needing any sign correction for the canvas's global Y-flip."""

    def _paint_shape(self, painter) -> None:
        painter.drawEllipse(self.boundingRect())

    def __init__(self, document: LayoutDocument, layout_scene, undo_stack, on_drag_update) -> None:
        super().__init__(document, layout_scene, undo_stack, on_drag_update)
        self.setCursor(Qt.PointingHandCursor)
        self._pivot: QPointF | None = None
        self._theta0 = 0.0
        self._rotation_at_press = 0.0

    def mousePressEvent(self, event) -> None:
        item = self._target_item()
        if item is None:
            return
        old_t = self.document.get_transform(self.inst_id)
        self._old_transform = old_t
        self._pivot = QPointF(old_t.x, old_t.y)
        mouse = event.scenePos()
        self._theta0 = math.degrees(math.atan2(mouse.y() - self._pivot.y(), mouse.x() - self._pivot.x()))
        self._rotation_at_press = old_t.rotation
        self.is_dragging = True
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self.is_dragging:
            return
        item = self._target_item()
        if item is None:
            return
        mouse = event.scenePos()
        theta_now = math.degrees(math.atan2(mouse.y() - self._pivot.y(), mouse.x() - self._pivot.x()))
        new_rotation = (self._rotation_at_press + (theta_now - self._theta0)) % 360.0

        item.apply_transform(item.pos().x(), item.pos().y(), new_rotation, item.mirror, item.mag)
        item.rotation_deg = new_rotation
        self._on_drag_update()
        event.accept()


class TransformHandleSet:
    """Owns the 4 scale handles + 1 rotate handle and keeps them
    positioned on whichever single instance is currently selected. Added
    to the scene once at construction and toggled with setVisible() rather
    than added/removed repeatedly — repositioning is driven by a polling
    timer (see MainWindow), same approach the old QWidget overlay used and
    for the same reason: simpler and harder to leave a gap in than hooking
    every individual view-mutating interaction (pan/zoom/resize/drag)."""

    ROTATE_HANDLE_OFFSET_PX = 24.0

    def __init__(self, scene, document: LayoutDocument, undo_stack) -> None:
        self.scene = scene
        self.document = document
        self.handles: list[_BaseHandle] = [
            ScaleHandleItem(i, document, scene, undo_stack, self.reposition) for i in range(4)
        ]
        self.rotate_handle = RotateHandleItem(document, scene, undo_stack, self.reposition)
        self.handles.append(self.rotate_handle)
        for h in self.handles:
            scene.addItem(h)
            h.setVisible(False)
        self._inst_id: int | None = None

    def is_interacting(self) -> bool:
        return any(h.is_dragging for h in self.handles)

    def show_for(self, inst_id: int) -> None:
        self._inst_id = inst_id
        for h in self.handles:
            h.inst_id = inst_id
            h.setVisible(True)
        self.reposition()

    def hide(self) -> None:
        self._inst_id = None
        for h in self.handles:
            h.setVisible(False)

    def reposition(self) -> None:
        if self._inst_id is None:
            return
        item = self.scene.items_by_inst.get(self._inst_id)
        if item is None:
            self.hide()
            return
        corners = _local_corners(item.boundingRect())
        for i, handle in enumerate(self.handles[:4]):
            handle.setPos(item.mapToScene(corners[i]))

        rect = item.boundingRect()
        top_center_local = QPointF((rect.left() + rect.right()) / 2, rect.top())
        # offset "above" in local +y by a few device pixels worth of scene
        # units at the view's current scale, so the handle sits a roughly
        # constant visual distance from the shape regardless of zoom
        view = self.scene.views()[0] if self.scene.views() else None
        scale = abs(view.transform().m22()) if view else 1.0
        offset_local = QPointF(top_center_local.x(), top_center_local.y() + self.ROTATE_HANDLE_OFFSET_PX / max(scale, 1e-6))
        self.rotate_handle.setPos(item.mapToScene(offset_local))
