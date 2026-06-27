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
# Routing/measure port-click tolerance in *screen pixels*, converted to scene
# units against the live zoom — a fixed micron radius is sub-pixel when zoomed
# out, which made port clicks essentially impossible to land.
_PORT_CLICK_PX = 16.0

# Speed of light in vacuum — used for spatial↔propagation-time conversion.
C0_UM_PER_FS: float = 0.299_792_458        # µm/fs
C0_UM_PER_NS: float = C0_UM_PER_FS * 1e6  # µm/ns  (≈ 299 792 µm/ns)

# Available display-unit modes shared by LayoutView and FieldView.
UNIT_MODES: list[tuple[str, str]] = [
    ("µm  (spatial)", "um"),
    ("nm  (spatial)", "nm"),
    ("fs  (propagation time)", "fs"),
    ("ns  (propagation time)", "ns"),
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
    route_pick_cancelled = Signal()  # Esc dropped a half-finished route's start port

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
        self._unit_mode: str = "um"   # "um" | "nm" | "fs" | "ns"
        self._n_eff: float = 1.0      # phase index for µm↔time conversion
        self._panning = False
        self._pan_last_pos = QPointF()
        self._drag_start_transforms: dict[int, Transform] = {}
        self._dimmed_route_ids: set[int] = set()  # routes faded during a drag (recomputed on drop)
        self.armed_component: str | None = None
        self.measure_mode = False
        self._measure_first_point: QPointF | None = None
        self._measure_items: list = []  # current annotation's QGraphicsItems
        self.source_mode = False
        self._source_markers: list = []  # accumulates across clicks, unlike measure mode
        # Routing feedback: a highlight over the port the cursor would snap to,
        # and a rubber-band track from the first picked port to the cursor.
        self._route_anchor: QPointF | None = None
        self._hover_port_item: QGraphicsEllipseItem | None = None
        self._route_preview_item: QGraphicsLineItem | None = None

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
        if not enabled:
            self.set_route_anchor(None)
            self._clear_hover_port()
        self.routing_mode_changed.emit(enabled)

    # -- routing visual feedback ------------------------------------------

    def set_route_anchor(self, scene_pt: QPointF | None) -> None:
        """Set (or clear) the first-picked port position. While set, a preview
        track is drawn from it to the cursor; cleared when the route completes
        or routing mode exits."""
        self._route_anchor = scene_pt
        if scene_pt is None and self._route_preview_item is not None:
            self.scene().removeItem(self._route_preview_item)
            self._route_preview_item = None

    def _update_hover_port(self, scene_pt: QPointF) -> QPointF | None:
        """Highlight the port the cursor would snap to (if any) and return its
        scene position so the preview track can lock onto it too."""
        hit = self._nearest_port_for_routing(scene_pt)
        if hit is None:
            self._clear_hover_port()
            return None
        inst_id, name = hit
        port_pt = self._port_scene_pos(inst_id, name)
        if port_pt is None:
            self._clear_hover_port()
            return None
        if self._hover_port_item is None:
            r = 7.0
            item = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            item.setPen(QPen(QColor("#00e0ff"), 2))
            item.setBrush(QColor(0, 224, 255, 70))
            item.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)  # constant on-screen size
            item.setZValue(2500.0)
            self.scene().addItem(item)
            self._hover_port_item = item
        self._hover_port_item.setVisible(True)
        self._hover_port_item.setPos(port_pt)
        return port_pt

    def _clear_hover_port(self) -> None:
        if self._hover_port_item is not None:
            self._hover_port_item.setVisible(False)

    def _port_scene_pos(self, inst_id: int, name: str) -> QPointF | None:
        item = self.scene().items_by_inst.get(inst_id)
        if item is None:
            return None
        for port_name, x, y in item._ports:
            if port_name == name:
                return item.mapToScene(QPointF(x, y))
        return None

    def _update_route_preview(self, end_pt: QPointF) -> None:
        if self._route_anchor is None:
            return
        if self._route_preview_item is None:
            line = QGraphicsLineItem()
            line.setPen(QPen(QColor("#00e0ff"), 0, Qt.DashLine))
            line.setZValue(2400.0)
            self.scene().addItem(line)
            self._route_preview_item = line
        self._route_preview_item.setLine(self._route_anchor.x(), self._route_anchor.y(), end_pt.x(), end_pt.y())

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

    def _port_click_tolerance_scene(self) -> float:
        """The port-click radius in scene µm for the current zoom — a fixed
        pixel target divided by the view's scale, so it stays grab-able at
        every zoom level."""
        return _PORT_CLICK_PX / max(abs(self.transform().m11()), 1e-9)

    def _nearest_port_for_routing(self, scene_pt: QPointF) -> tuple[int, str] | None:
        """The (inst_id, port_name) of the port nearest scene_pt within the
        zoom-aware click tolerance, or None. Drives routing-mode clicks at the
        view level so they don't depend on hitting a thin item's exact shape."""
        tol = self._port_click_tolerance_scene()
        best: tuple[int, str] | None = None
        best_dist2 = tol * tol
        for inst_id, item in self.scene().items_by_inst.items():
            for name, x, y in item._ports:
                sp = item.mapToScene(QPointF(x, y))
                dist2 = (sp.x() - scene_pt.x()) ** 2 + (sp.y() - scene_pt.y()) ** 2
                if dist2 <= best_dist2:
                    best_dist2 = dist2
                    best = (inst_id, name)
        return best

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

    def cancel_current_action(self) -> bool:
        """Back out of whatever interactive action is in progress, most-transient
        first. Routing is two-stage: the first call drops a half-finished
        route (the picked start port) but stays in routing mode; the next call
        exits routing mode. Returns whether anything was cancelled.

        Shared by the canvas's own Esc key (keyPressEvent) and a window-level
        Esc shortcut, so cancelling works no matter which widget has focus —
        e.g. right after arming a placement from the palette."""
        if self.armed_component is not None:
            self.cancel_placement()
            return True
        if self.scene().routing_mode:
            if self._route_anchor is not None:
                self.set_route_anchor(None)  # drop the picked start port, stay in routing mode
                self._clear_hover_port()
                self.route_pick_cancelled.emit()
                return True
            self.set_routing_mode(False)
            return True
        if self.measure_mode:
            self.set_measure_mode(False)
            return True
        if self.source_mode:
            self.set_source_mode(False)
            return True
        return False

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and self.cancel_current_action():
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
        if self.scene().routing_mode and event.button() == Qt.LeftButton:
            # Handle routing clicks here, zoom-aware, rather than relying on a
            # click landing on an instance item's thin geometry within a fixed
            # micron radius (impossible when zoomed out). Always accept so a
            # near-miss doesn't fall through to selection/drag.
            scene_pt = self.mapToScene(event.position().toPoint())
            hit = self._nearest_port_for_routing(scene_pt)
            if hit is not None:
                self.scene().port_clicked.emit(*hit)
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
        if self.scene().routing_mode:
            scene_pt = self.mapToScene(QPoint(int(pos.x()), int(pos.y())))
            snap_pt = self._update_hover_port(scene_pt)
            self._update_route_preview(snap_pt or scene_pt)
            super().mouseMoveEvent(event)
            return
        super().mouseMoveEvent(event)
        # Live snap: re-apply the same port/grid snap the drop uses, every move,
        # so the selection visibly snaps mid-drag instead of jumping on release.
        # Qt anchors each item to its button-down position + total mouse delta,
        # not to the snapped pos we set here, so re-snapping every frame is
        # stable and doesn't drift.
        if self._drag_start_transforms and (event.buttons() & Qt.LeftButton):
            self._dim_connected_routes()  # fade attached tracks so the stale path reads as "will update"
            self._snap_dragged_items()

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

        self._snap_dragged_items()

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

        # Rerouted tracks come back as fresh (full-opacity) items; restore any
        # faded route that wasn't rebuilt (e.g. the drag ended where it started).
        self._restore_dimmed_routes()

    def _dim_connected_routes(self) -> None:
        """Fade the scene items of routes attached to the dragged instances, so
        their now-stale path reads as 'will be recomputed on drop' rather than a
        frozen UI. Idempotent: only fades routes not already faded this drag."""
        scene = self.scene()
        document = getattr(scene, "document", None)
        if document is None:
            return
        for inst_id in self._drag_start_transforms:
            for route_id in document.routes_for_instance(inst_id):
                if route_id in self._dimmed_route_ids:
                    continue
                item = scene.route_items.get(route_id)
                if item is not None:
                    item.setOpacity(0.3)
                    self._dimmed_route_ids.add(route_id)

    def _restore_dimmed_routes(self) -> None:
        scene = self.scene()
        for route_id in self._dimmed_route_ids:
            item = scene.route_items.get(route_id)
            if item is not None:
                item.setOpacity(1.0)
        self._dimmed_route_ids.clear()

    def _snap_dragged_items(self) -> None:
        """Snap the currently-dragged items: align to a nearby port if one is
        within range (shifting the whole drag group by one offset so their
        relative arrangement is preserved), else grid-snap each. Used both
        live during a drag and on release."""
        if not self.snap_enabled:
            return
        scene = self.scene()
        dirty_ids = scene.dirty_instance_ids()
        if not dirty_ids:
            return
        offset = self._find_port_snap_offset(dirty_ids)
        if offset is not None:
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
        """Switch axis labels between 'um', 'nm', 'fs', or 'ns'."""
        self._unit_mode = mode
        self.viewport().update()

    def set_n_eff(self, n: float) -> None:
        """Update the effective phase index used for µm↔time conversion."""
        self._n_eff = max(n, 1e-6)
        if self._unit_mode in ("fs", "ns"):
            self.viewport().update()

    @property
    def n_eff(self) -> float:
        """The effective phase index currently driving propagation-time display
        (seeded from the core index, updated by the last FDTD mode solve)."""
        return self._n_eff

    def um_to_display(self, um: float) -> float:
        """Convert a µm scene coordinate to the current display unit value."""
        if self._unit_mode == "nm":
            return um * 1000.0
        if self._unit_mode == "fs":
            return um * self._n_eff / C0_UM_PER_FS
        if self._unit_mode == "ns":
            return um * self._n_eff / C0_UM_PER_NS
        return um

    def display_to_um(self, val: float) -> float:
        """Inverse: display unit value → µm scene coordinate."""
        if self._unit_mode == "nm":
            return val / 1000.0
        if self._unit_mode == "fs":
            return val * C0_UM_PER_FS / self._n_eff
        if self._unit_mode == "ns":
            return val * C0_UM_PER_NS / self._n_eff
        return val

    def unit_str(self) -> str:
        """Short label for the current display unit, including n_eff when relevant."""
        if self._unit_mode == "nm":
            return "nm"
        if self._unit_mode == "fs":
            return f"fs (n={self._n_eff:.3f})"
        if self._unit_mode == "ns":
            return f"ns (n={self._n_eff:.3f})"
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
