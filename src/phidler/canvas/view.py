from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QUndoStack
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from phidler.model.document import Transform

from .scene import LayoutScene

_MIN_SCALE = 0.01
_MAX_SCALE = 2000.0
_PORT_SNAP_THRESHOLD_UM = 2.0  # micron-space, independent of zoom level

# Speed of light in vacuum [µm/fs] — used for spatial↔propagation-time conversion.
C0_UM_PER_FS: float = 0.299_792_458

# Available display-unit modes shared by LayoutView and FieldView.
UNIT_MODES: list[tuple[str, str]] = [
    ("µm  (spatial)", "um"),
    ("fs  (propagation time in vacuum)", "fs"),
]


def nice_ticks(lo: float, hi: float, max_ticks: int = 7) -> list[float]:
    """Human-readable tick positions spanning [lo, hi] with at most max_ticks values."""
    span = hi - lo
    if span <= 0 or not math.isfinite(span) or not math.isfinite(lo):
        return []
    raw_step = span / max_ticks
    try:
        magnitude = 10.0 ** math.floor(math.log10(raw_step))
    except ValueError:
        return []
    step = magnitude * 10
    for mult in (1, 2, 5, 10):
        if span / (magnitude * mult) <= max_ticks:
            step = magnitude * mult
            break
    first = math.ceil(lo / step) * step
    ticks, val = [], first
    while val <= hi + step * 1e-9:
        ticks.append(round(val / step) * step)
        val += step
    return ticks


class LayoutView(QGraphicsView):
    """Pan/zoom/grid canvas for a LayoutScene.

    Applies a single global Y-flip so GDS's Y-up coordinates display with
    "up" being up on screen; all scene/item-space math elsewhere treats
    coordinates as plain GDS microns, with this view doing the only flip.
    """

    instances_moved = Signal(list)  # list[int] of instance ids committed
    placement_requested = Signal(str, float, float)  # component_spec, x, y
    placement_armed_changed = Signal(bool)
    routing_mode_changed = Signal(bool)
    measure_mode_changed = Signal(bool)
    measurement_taken = Signal(float, float, float)  # dx, dy, distance (all microns)
    source_mode_changed = Signal(bool)
    source_placement_requested = Signal(float, float)  # scene x, y in microns
    context_menu_requested = Signal(QPoint)  # viewport-pixel position
    cursor_position_changed = Signal(float, float)  # scene x, y in microns

    def __init__(self, scene: LayoutScene, parent=None, undo_stack: QUndoStack | None = None) -> None:
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.scale(1, -1)  # the one global Y-flip

        self.undo_stack = undo_stack
        self.grid_pitch = 1.0  # microns
        self.snap_enabled = True
        self._unit_mode: str = "um"   # "um" | "fs"
        self._n_eff: float = 1.0      # phase index for µm↔fs conversion
        self._panning = False
        self._pan_last_pos = QPointF()
        self._drag_start_transforms: dict[int, Transform] = {}
        self.armed_component: str | None = None
        self.measure_mode = False
        self._measure_first_point: QPointF | None = None
        self._measure_items: list = []  # current annotation's QGraphicsItems
        self.source_mode = False
        self._source_markers: list = []  # accumulates across clicks, unlike measure mode

    # -- placement mode ---------------------------------------------------

    def arm_placement(self, component_spec: str) -> None:
        self.armed_component = component_spec
        self.setCursor(Qt.CrossCursor)
        self.placement_armed_changed.emit(True)

    def cancel_placement(self) -> None:
        self.armed_component = None
        self.setCursor(Qt.ArrowCursor)
        self.placement_armed_changed.emit(False)

    def set_routing_mode(self, enabled: bool) -> None:
        if enabled:
            self.set_measure_mode(False)
            self.set_source_mode(False)
        self.scene().routing_mode = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.routing_mode_changed.emit(enabled)

    # -- measure mode -------------------------------------------------------

    def set_measure_mode(self, enabled: bool) -> None:
        if enabled:
            self.set_routing_mode(False)
            self.set_source_mode(False)
            self.cancel_placement()
        else:
            self._clear_measurement()
        self.measure_mode = enabled
        self._measure_first_point = None
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.measure_mode_changed.emit(enabled)

    # -- source-placement mode -----------------------------------------------

    def set_source_mode(self, enabled: bool) -> None:
        """Click-to-place mode for FDTD source markers, used by FdtdWindow.
        Markers accumulate across clicks (unlike measure mode's single
        replaced annotation) since the use case is placing several
        sources — clearing them is the caller's job via
        clear_source_markers(), not implicit on mode toggle."""
        if enabled:
            self.set_routing_mode(False)
            self.set_measure_mode(False)
            self.cancel_placement()
        self.source_mode = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.source_mode_changed.emit(enabled)

    def add_source_marker(self, x: float, y: float) -> QGraphicsItem:
        """Adds a small constant-pixel-size marker glyph at the given scene
        position and returns it, so the caller (FdtdWindow) can track one
        marker per source row and remove it later via remove_source_marker."""
        marker = QGraphicsEllipseItem(-5, -5, 10, 10)
        marker.setBrush(QColor("#ffaa00"))
        marker.setPen(QPen(QColor("#664400"), 1))
        marker.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        marker.setZValue(2000.0)
        marker.setPos(x, y)
        self.scene().addItem(marker)
        self._source_markers.append(marker)
        return marker

    def remove_source_marker(self, marker: QGraphicsItem) -> None:
        if marker in self._source_markers:
            self._source_markers.remove(marker)
        self.scene().removeItem(marker)

    def clear_source_markers(self) -> None:
        for marker in self._source_markers:
            self.scene().removeItem(marker)
        self._source_markers = []

    def _handle_source_click(self, viewport_pos: QPoint) -> None:
        raw_scene_pt = self.mapToScene(viewport_pos)
        scene_pt = self._nearest_port_scene_point(raw_scene_pt) or raw_scene_pt
        self.source_placement_requested.emit(scene_pt.x(), scene_pt.y())

    def _clear_measurement(self) -> None:
        for item in self._measure_items:
            self.scene().removeItem(item)
        self._measure_items = []

    def _nearest_port_scene_point(self, scene_pt: QPointF) -> QPointF | None:
        """If scene_pt is within port-click range of any instance's port,
        return that port's exact scene position instead of the raw click
        point — reuses the same nearest_port()/hit-radius logic routing
        mode already uses for port clicks, so measuring between two ports
        lands on their exact centers rather than wherever you clicked near
        one."""
        for item in self.scene().items_by_inst.values():
            local_pt = item.mapFromScene(scene_pt)
            port_name = item.nearest_port(local_pt)
            if port_name is None:
                continue
            for name, x, y in item._ports:
                if name == port_name:
                    return item.mapToScene(QPointF(x, y))
        return None

    def _handle_measure_click(self, viewport_pos: QPoint) -> None:
        raw_scene_pt = self.mapToScene(viewport_pos)
        scene_pt = self._nearest_port_scene_point(raw_scene_pt) or raw_scene_pt

        if self._measure_first_point is None:
            self._clear_measurement()
            self._measure_first_point = scene_pt
            return

        p1 = self._measure_first_point
        p2 = scene_pt
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        distance = math.hypot(dx, dy)
        self._draw_measurement(p1, p2, dx, dy, distance)
        self.measurement_taken.emit(dx, dy, distance)
        self._measure_first_point = None

    def _draw_measurement(self, p1: QPointF, p2: QPointF, dx: float, dy: float, distance: float) -> None:
        self._clear_measurement()
        pen = QPen(QColor("#00e0ff"), 0, Qt.DashLine)
        line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
        line.setPen(pen)
        line.setZValue(2000.0)
        self.scene().addItem(line)

        label = QGraphicsSimpleTextItem(f"{distance:.3f} µm  (dx={dx:.3f}, dy={dy:.3f})")
        label.setBrush(QColor("#00e0ff"))
        label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        label.setZValue(2000.0)
        mid = QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
        label.setPos(mid)
        self.scene().addItem(label)

        self._measure_items = [line, label]

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            if self.armed_component is not None:
                self.cancel_placement()
                event.accept()
                return
            if self.scene().routing_mode:
                self.set_routing_mode(False)
                event.accept()
                return
            if self.measure_mode:
                self.set_measure_mode(False)
                event.accept()
                return
            if self.source_mode:
                self.set_source_mode(False)
                event.accept()
                return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(event.pos())

    # -- zoom -----------------------------------------------------------

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        current_scale = abs(self.transform().m11())
        new_scale = current_scale * factor
        if new_scale < _MIN_SCALE or new_scale > _MAX_SCALE:
            return
        self.scale(factor, factor)

    # -- pan (middle-mouse-drag) -----------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last_pos = event.position() if hasattr(event, "position") else event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if self.armed_component is not None and event.button() == Qt.LeftButton:
            scene_pt = self.mapToScene(event.position().toPoint())
            x, y = self.snap(scene_pt.x()), self.snap(scene_pt.y())
            component_spec = self.armed_component
            self.cancel_placement()
            self.placement_requested.emit(component_spec, x, y)
            event.accept()
            return
        if self.measure_mode and event.button() == Qt.LeftButton:
            self._handle_measure_click(event.position().toPoint())
            event.accept()
            return
        if self.source_mode and event.button() == Qt.LeftButton:
            self._handle_source_click(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)
        self._drag_start_transforms = {
            item.inst_id: self.scene().document.get_transform(item.inst_id) for item in self.scene().selectedItems()
        }

    def mouseMoveEvent(self, event) -> None:
        pos = event.position() if hasattr(event, "position") else event.pos()
        self.report_cursor_position(QPoint(int(pos.x()), int(pos.y())))
        if self._panning:
            delta = pos - self._pan_last_pos
            self._pan_last_pos = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def report_cursor_position(self, viewport_pos: QPoint) -> None:
        """Pure coordinate transform, split out from mouseMoveEvent so it
        can be tested by calling it directly with a known point instead of
        injecting a synthetic QMouseEvent — confirmed elsewhere in this
        codebase that synthetic native event injection under the offscreen
        platform can be unstable (a QContextMenuEvent sent via
        QApplication.sendEvent segfaulted the interpreter), so cursor
        coordinate correctness is verified via this direct call instead."""
        scene_pt = self.mapToScene(viewport_pos)
        self.cursor_position_changed.emit(scene_pt.x(), scene_pt.y())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        scene = self.scene()

        if self.snap_enabled:
            dirty_ids = scene.dirty_instance_ids()
            offset = self._find_port_snap_offset(dirty_ids)
            if offset is not None:
                # Port-to-port match found: shift every dragged item by the
                # same offset (preserving their arrangement relative to each
                # other in a multi-select drag) so the matched ports align
                # exactly, instead of independently grid-snapping each one
                # (which could round away from that alignment).
                dx, dy = offset
                for inst_id in dirty_ids:
                    item = scene.items_by_inst.get(inst_id)
                    if item is not None:
                        pos = item.pos()
                        item.setPos(pos.x() + dx, pos.y() + dy)
            else:
                for inst_id in dirty_ids:
                    item = scene.items_by_inst.get(inst_id)
                    if item is not None:
                        pos = item.pos()
                        item.setPos(self.snap(pos.x()), self.snap(pos.y()))

        committed = scene.commit_dirty_transforms()
        if committed:
            if self.undo_stack is not None:
                from phidler.model.commands import MoveInstanceCommand

                for inst_id in committed:
                    old_t = self._drag_start_transforms.get(inst_id)
                    new_t = scene.document.get_transform(inst_id)
                    if old_t is not None and (old_t.x, old_t.y, old_t.rotation, old_t.mirror) != (
                        new_t.x,
                        new_t.y,
                        new_t.rotation,
                        new_t.mirror,
                    ):
                        self.undo_stack.push(MoveInstanceCommand(scene.document, scene, inst_id, old_t, new_t))
            self._drag_start_transforms = {}
            self.instances_moved.emit(committed)

    def _find_port_snap_offset(self, dragged_ids: list[int]) -> tuple[float, float] | None:
        """Finds the closest (dragged port, other instance's port) pair
        within _PORT_SNAP_THRESHOLD_UM and returns the (dx, dy) offset
        that would align them exactly, or None if nothing is close enough.

        Orientation isn't considered — this snaps on proximity alone, not
        whether the two ports actually face each other. A documented v1
        simplification, not an oversight: matching by direction too would
        be a reasonable follow-up, not required for "snapping is easier"."""
        if not dragged_ids:
            return None
        document = self.scene().document
        items_by_inst = self.scene().items_by_inst
        dragged_set = set(dragged_ids)

        target_ports: list[tuple[float, float]] = []
        for inst_id in document.instances:
            if inst_id in dragged_set:
                continue
            for _name, x, y in document.get_absolute_ports_for_instance(inst_id):
                target_ports.append((x, y))
        if not target_ports:
            return None

        dragged_ports: list[tuple[float, float]] = []
        for inst_id in dragged_ids:
            item = items_by_inst.get(inst_id)
            if item is None:
                continue
            old_t = self._drag_start_transforms.get(inst_id) or document.get_transform(inst_id)
            pos = item.pos()
            for _name, x, y in document.get_absolute_ports_for_transform(
                inst_id, pos.x(), pos.y(), old_t.rotation, old_t.mirror, old_t.mag
            ):
                dragged_ports.append((x, y))
        if not dragged_ports:
            return None

        best_dist_sq = _PORT_SNAP_THRESHOLD_UM**2
        best_offset: tuple[float, float] | None = None
        for dx_pt, dy_pt in dragged_ports:
            for tx, ty in target_ports:
                dist_sq = (tx - dx_pt) ** 2 + (ty - dy_pt) ** 2
                if dist_sq <= best_dist_sq:
                    best_dist_sq = dist_sq
                    best_offset = (tx - dx_pt, ty - dy_pt)
        return best_offset

    def snap(self, value: float) -> float:
        if not self.snap_enabled or self.grid_pitch <= 0:
            return value
        return round(value / self.grid_pitch) * self.grid_pitch

    # -- zoom to fit / selection ------------------------------------------

    def zoom_to_fit(self) -> None:
        self._fit_to_rect(self.scene().itemsBoundingRect())

    def zoom_to_selection(self) -> None:
        selected = self.scene().selectedItems()
        if not selected:
            return
        rect = QRectF()
        for item in selected:
            rect = rect.united(item.sceneBoundingRect())
        self._fit_to_rect(rect)

    def _fit_to_rect(self, rect: QRectF) -> None:
        if rect.isEmpty():
            return
        margin = max(rect.width(), rect.height()) * 0.1 or 1.0
        padded = rect.adjusted(-margin, -margin, margin, margin)
        # fitInView composes onto the existing transform (verified
        # empirically: it preserves the view's global Y-flip rather than
        # replacing the transform outright, so no re-flip is needed here).
        self.fitInView(padded, Qt.KeepAspectRatio)

    # -- coordinate unit helpers ------------------------------------------

    def set_unit_mode(self, mode: str) -> None:
        """Switch axis labels between 'um' (spatial) and 'fs' (propagation time)."""
        self._unit_mode = mode
        self.viewport().update()

    def set_n_eff(self, n: float) -> None:
        """Update the effective phase index used for µm↔fs conversion."""
        self._n_eff = max(n, 1e-6)
        if self._unit_mode == "fs":
            self.viewport().update()

    def um_to_display(self, um: float) -> float:
        """Convert a µm scene coordinate to the current display unit value."""
        return um * self._n_eff / C0_UM_PER_FS if self._unit_mode == "fs" else um

    def display_to_um(self, val: float) -> float:
        """Inverse: display unit value → µm scene coordinate."""
        return val * C0_UM_PER_FS / self._n_eff if self._unit_mode == "fs" else val

    def unit_str(self) -> str:
        """Short label for the current display unit, including n_eff when relevant."""
        if self._unit_mode == "fs":
            return f"fs (n={self._n_eff:.3f})"
        return "µm"

    # -- grid + foreground labels -----------------------------------------

    def drawForeground(self, painter, rect: QRectF) -> None:
        """Draw axis coordinate labels in the current display unit."""
        painter.save()
        painter.resetTransform()          # switch to viewport pixel coordinates

        vp = self.viewport()
        vp_w, vp_h = vp.width(), vp.height()

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        fm = painter.fontMetrics()

        # Compute nice tick positions in display units over the visible scene rect
        xmin_d = self.um_to_display(rect.left())
        xmax_d = self.um_to_display(rect.right())
        ymin_d = self.um_to_display(rect.top())
        ymax_d = self.um_to_display(rect.bottom())

        x_ticks_d = nice_ticks(xmin_d, xmax_d)
        y_ticks_d = nice_ticks(ymin_d, ymax_d)

        label_color = QColor("#aaaaaa")
        shadow_color = QColor(0, 0, 0, 140)

        def draw_shadowed(x_px: int, y_px: int, text: str) -> None:
            painter.setPen(QPen(shadow_color))
            for ddx, ddy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                painter.drawText(x_px + ddx, y_px + ddy, text)
            painter.setPen(QPen(label_color))
            painter.drawText(x_px, y_px, text)

        margin_l = 36
        margin_b = 14

        # X-axis labels along the bottom edge
        for x_d in x_ticks_d:
            x_um = self.display_to_um(x_d)
            px = int(self.mapFromScene(QPointF(x_um, 0)).x())
            if not (margin_l <= px <= vp_w - 5):
                continue
            label = f"{x_d:.4g}"
            tw = fm.horizontalAdvance(label)
            draw_shadowed(px - tw // 2, vp_h - 3, label)

        # Y-axis labels along the left edge
        for y_d in y_ticks_d:
            y_um = self.display_to_um(y_d)
            py = int(self.mapFromScene(QPointF(0, y_um)).y())
            if not (5 <= py <= vp_h - margin_b):
                continue
            draw_shadowed(4, py + fm.ascent() // 2, f"{y_d:.4g}")

        # Unit suffix centred at bottom-right
        unit = self.unit_str()
        painter.setPen(QPen(QColor("#666666")))
        uw = fm.horizontalAdvance(unit)
        painter.drawText(vp_w - uw - 6, vp_h - 3, unit)

        painter.restore()

    def drawBackground(self, painter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor("#1e1e1e"))

        if self.grid_pitch <= 0:
            return  # grid_pitch is user-configurable; <= 0 would loop forever below

        pitch = self.grid_pitch
        # avoid drawing a degenerate flood of lines when zoomed far out
        view_scale = abs(self.transform().m11())
        while pitch * view_scale < 6:
            pitch *= 10
        if pitch * view_scale > 4000:
            return

        left = int(rect.left() / pitch) - 1
        right = int(rect.right() / pitch) + 1
        top = int(rect.top() / pitch) - 1
        bottom = int(rect.bottom() / pitch) + 1

        pen = QPen(QColor("#3a3a3a"), 0)
        painter.setPen(pen)
        for i in range(left, right + 1):
            x = i * pitch
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for j in range(top, bottom + 1):
            y = j * pitch
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        axis_pen = QPen(QColor("#5a5a5a"), 0)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(rect.left(), 0), QPointF(rect.right(), 0))
        painter.drawLine(QPointF(0, rect.top()), QPointF(0, rect.bottom()))
