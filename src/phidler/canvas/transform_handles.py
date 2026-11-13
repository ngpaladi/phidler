from __future__ import annotations

import math

import inspect

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPolygonItem

from phidler.model.document import LayoutDocument, Transform

_HANDLE_PIXELS = 9.0  # constant on-screen size regardless of zoom
_HANDLE_FILL = QColor("#ffd400")
_HANDLE_PEN = QPen(QColor("#000000"), 1)
_MIN_MAG = 0.05
_MAX_MAG = 50.0


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


# The shapes whose outline follows one parameter exactly (checked for each in
# the tests). Anything else in the shapes group is resized by magnification:
# its "size" may mean something else (fiducial_squares) or its outline may
# include more than the radius (circle_wave).
_SIZE_PARAMETER = {
    "rectangle": "size",
    "rectangles": "size",
    "compass": "size",
    "L": "size",
    "C": "size",
    "ellipse": "radii",
    "circle": "radius",
    "pie_arc": "radius",
}


def _is_pair(value) -> bool:
    return (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def resize_mode(document: LayoutDocument, inst_id: int) -> tuple[str, object] | None:
    """How dragging a corner should resize this instance, with the value it
    starts from, or None when it shouldn't be drag-resized at all.

    A shape is resized through the parameter that sets its extent, so the result
    is a real rectangle/ellipse/circle of the new size rather than a magnified
    copy: ``("size", (w, h))`` and ``("radii", (rx, ry))`` stretch each axis on
    its own, ``("radius", r)`` scales evenly. Shapes without such a parameter
    fall back to ``("mag", m)``, the instance's magnification.
    Photonic components get no corner handles: stretching one would silently
    change its waveguide widths and gaps."""
    inst = document.instances.get(inst_id)
    if inst is None or inst.array.columns * inst.array.rows > 1:
        return None
    import gdsfactory as gf

    factory = gf.get_active_pdk().cells.get(inst.component_spec)
    if factory is None:
        return None
    # Only gdsfactory's "shapes" group: a bend or a ring also has a radius, but
    # dragging it would change the device, not just a drawn outline.
    module = (getattr(factory, "__module__", "") or "").split(".")
    if module[:3] != ["gdsfactory", "components", "shapes"]:
        return None
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return None

    def current(name: str):
        if name in inst.kwargs:
            return inst.kwargs[name]
        p = params.get(name)
        return None if p is None or p.default is inspect.Parameter.empty else p.default

    name = _SIZE_PARAMETER.get(inst.component_spec)
    value = current(name) if name in params else None
    if name in ("size", "radii") and _is_pair(value):
        return name, tuple(float(v) for v in value)
    if name == "radius" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return name, float(value)
    return "mag", float(document.get_transform(inst_id).mag)


class ResizeHandleItem(_BaseHandle):
    """One corner of the selected shape's bounding box. Dragging it resizes the
    shape with the opposite corner held in place (see resize_mode for what
    "resize" changes). Hold Shift to keep the proportions.

    The drag shows a dashed outline of the new extent; the shape itself is
    rebuilt once, on release, as a single undo step. That keeps the drag smooth
    and doesn't leave a trail of throwaway cells behind."""

    _MIN_EXTENT_UM = 0.01

    def __init__(self, document, layout_scene, undo_stack, on_drag_update, corner, on_resized=None) -> None:
        super().__init__(document, layout_scene, undo_stack, on_drag_update)
        self.corner = corner  # (0|1, 0|1): which end of the local bounds in x and in y
        self._on_resized = on_resized
        # The canvas is y-up: corners (0,0) and (1,1) are bottom-left and top-right
        # on screen for an unrotated shape, the "/" diagonal.
        self.setCursor(Qt.SizeBDiagCursor if corner[0] == corner[1] else Qt.SizeFDiagCursor)
        self.preview = QGraphicsPolygonItem()
        pen = QPen(QColor("#ffd400"), 0, Qt.DashLine)
        self.preview.setPen(pen)
        self.preview.setZValue(999.0)
        self.preview.setVisible(False)
        self._mode: tuple[str, object] | None = None
        self._pending = None  # the new parameter value / magnification

    def _paint_shape(self, painter) -> None:
        painter.drawRect(self.boundingRect())

    @staticmethod
    def corner_point(rect: QRectF, corner: tuple[int, int]) -> QPointF:
        return QPointF(rect.right() if corner[0] else rect.left(), rect.bottom() if corner[1] else rect.top())

    def _snap(self, value: float, start: float) -> float:
        """`value` on the view's grid. Never below one grid pitch (or the size
        the shape started with, if that was smaller), so dragging onto or past
        the fixed corner can't collapse the shape to nothing."""
        views = self.layout_scene.views()
        view = views[0] if views else None
        pitch = getattr(view, "grid_pitch", 0.0) if getattr(view, "snap_enabled", False) else 0.0
        if pitch > 0:
            return max(view.snap(value), min(start, pitch))
        return max(value, min(start, self._MIN_EXTENT_UM))

    def mousePressEvent(self, event) -> None:
        item = self._target_item()
        self._mode = resize_mode(self.document, self.inst_id) if item is not None else None
        if self._mode is None:
            return
        rect = item.boundingRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self._rect0 = QRectF(rect)
        self._anchor_corner = (1 - self.corner[0], 1 - self.corner[1])
        self._anchor_local = self.corner_point(rect, self._anchor_corner)
        self._anchor_scene = item.mapToScene(self._anchor_local)
        self._pending = None
        self.is_dragging = True
        if self.preview.scene() is None:
            self.layout_scene.addItem(self.preview)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        item = self._target_item()
        if not self.is_dragging or item is None:
            return
        kind, start = self._mode
        local = item.mapFromScene(event.scenePos())
        w0, h0 = self._rect0.width(), self._rect0.height()
        # Distance from the fixed corner, measured towards the dragged one.
        dx = (local.x() - self._anchor_local.x()) * (1 if self.corner[0] else -1)
        dy = (local.y() - self._anchor_local.y()) * (1 if self.corner[1] else -1)
        fx, fy = max(dx, 0.0) / w0, max(dy, 0.0) / h0
        modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.NoModifier
        if kind in ("radius", "mag") or modifiers & Qt.ShiftModifier:
            fx = fy = max(fx, fy)

        if kind == "mag":
            mag = min(max(start * fx, _MIN_MAG), _MAX_MAG)
            fx = fy = mag / start
            self._pending = mag
        elif kind == "radius":
            radius = self._snap(start * fx, start)
            fx = fy = radius / start
            self._pending = radius
        elif kind == "radii":
            new = (self._snap(start[0] * fx, start[0]), self._snap(start[1] * fy, start[1]))
            fx, fy = new[0] / start[0], new[1] / start[1]
            self._pending = new
        else:
            # "size": the outline is the size plus a fixed margin (none for a
            # rectangle, half the line width for an L), so add the change in
            # extent to the size rather than scaling it.
            new = (
                self._snap(start[0] + w0 * (fx - 1), start[0]),
                self._snap(start[1] + h0 * (fy - 1), start[1]),
            )
            fx, fy = 1 + (new[0] - start[0]) / w0, 1 + (new[1] - start[1]) / h0
            self._pending = new

        sx = w0 * fx * (1 if self.corner[0] else -1)
        sy = h0 * fy * (1 if self.corner[1] else -1)
        a = self._anchor_local
        outline = [a, QPointF(a.x() + sx, a.y()), QPointF(a.x() + sx, a.y() + sy), QPointF(a.x(), a.y() + sy)]
        self.preview.setPolygon(QPolygonF([item.mapToScene(pt) for pt in outline]))
        self.preview.setVisible(True)
        self.setPos(item.mapToScene(outline[2]))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if not self.is_dragging:
            return
        self.is_dragging = False
        self.preview.setVisible(False)
        kind, start = self._mode
        pending = self._pending
        self._pending = None
        if pending is not None and pending != start and self._target_item() is not None:
            self._commit(kind, pending)
        self._on_drag_update()
        event.accept()

    def _commit(self, kind: str, value) -> None:
        from phidler.model.commands import EditParamsCommand, MoveInstanceCommand

        inst_id = self.inst_id
        stack = self.undo_stack
        stack.beginMacro("Resize")
        failed = False
        if kind == "mag":
            t = self.document.get_transform(inst_id)
            scaled = Transform(x=t.x, y=t.y, rotation=t.rotation, mirror=t.mirror, mag=value)
            stack.push(MoveInstanceCommand(self.document, self.layout_scene, inst_id, t, scaled, "Scale"))
        else:
            old_kwargs = dict(self.document.instances[inst_id].kwargs)
            command = EditParamsCommand(self.document, self.layout_scene, inst_id, old_kwargs, {**old_kwargs, kind: value})
            stack.push(command)
            failed = command.error is not None
        if not failed:
            # Put the fixed corner back where it was: the rebuilt shape need not
            # grow from the same point (a centred rectangle grows both ways).
            item = self.layout_scene.items_by_inst[inst_id]
            now = item.mapToScene(self.corner_point(item.boundingRect(), self._anchor_corner))
            dx, dy = self._anchor_scene.x() - now.x(), self._anchor_scene.y() - now.y()
            if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                t = self.document.get_transform(inst_id)
                moved = Transform(
                    x=round(t.x + dx, 6), y=round(t.y + dy, 6), rotation=t.rotation, mirror=t.mirror, mag=t.mag
                )
                stack.push(MoveInstanceCommand(self.document, self.layout_scene, inst_id, t, moved))
        stack.endMacro()
        # The position was set through the document; it isn't a canvas drag for
        # the view to snap and record again when this mouse release reaches it.
        self.layout_scene.clear_dirty(inst_id)
        if failed:
            stack.undo()
        elif self._on_resized is not None:
            self._on_resized(inst_id)


class TransformHandleSet:
    """Owns the on-canvas rotate handle and keeps it positioned on whichever
    single instance is currently selected. Added to the scene once at
    construction and toggled with setVisible() rather than added/removed
    repeatedly — repositioning is driven by a polling timer (see MainWindow),
    same approach the old QWidget overlay used and for the same reason: simpler
    and harder to leave a gap in than hooking every individual view-mutating
    interaction (pan/zoom/resize/drag).

    Shapes (rectangle, ellipse, circle, …) also get a handle on each corner of
    their bounding box for drag-resizing; see resize_mode for which instances
    qualify. Photonic components are deliberately left without them, so a stray
    corner drag can't distort a device: their scale stays a number in the
    Properties panel."""

    ROTATE_HANDLE_OFFSET_PX = 24.0

    def __init__(self, scene, document: LayoutDocument, undo_stack, on_resized=None) -> None:
        self.scene = scene
        self.document = document
        self.rotate_handle = RotateHandleItem(document, scene, undo_stack, self.reposition)
        self.resize_handles = [
            ResizeHandleItem(document, scene, undo_stack, self.reposition, corner, on_resized)
            for corner in ((0, 0), (1, 0), (1, 1), (0, 1))
        ]
        self.handles: list[_BaseHandle] = [self.rotate_handle, *self.resize_handles]
        for h in self.handles:
            scene.addItem(h)
            h.setVisible(False)
        self._inst_id: int | None = None

    def is_interacting(self) -> bool:
        return any(h.is_dragging for h in self.handles)

    def show_for(self, inst_id: int) -> None:
        self._inst_id = inst_id
        resizable = resize_mode(self.document, inst_id) is not None
        for h in self.handles:
            h.inst_id = inst_id
            h.setVisible(resizable or h is self.rotate_handle)
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
        rect = item.boundingRect()
        # The canvas is y-up, so the edge drawn on top is the rect's largest y.
        top_center_local = QPointF((rect.left() + rect.right()) / 2, rect.bottom())
        # offset "above" in local +y by a few device pixels worth of scene
        # units at the view's current scale, so the handle sits a roughly
        # constant visual distance from the shape regardless of zoom
        view = self.scene.views()[0] if self.scene.views() else None
        scale = abs(view.transform().m22()) if view else 1.0
        offset_local = QPointF(top_center_local.x(), top_center_local.y() + self.ROTATE_HANDLE_OFFSET_PX / max(scale, 1e-6))
        self.rotate_handle.setPos(item.mapToScene(offset_local))
        for h in self.resize_handles:
            if h.isVisible():
                h.setPos(item.mapToScene(ResizeHandleItem.corner_point(rect, h.corner)))
