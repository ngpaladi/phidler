from __future__ import annotations

from PySide6.QtCore import QRectF, Signal
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene

from phidler.model.document import LayoutDocument

from .annotation_item import AnnotationItem
from .polygon_item import InstanceItem

_VIOLATION_PEN = QPen(QColor("#ff0000"), 0)
_VIOLATION_BRUSH = QBrush(QColor(255, 0, 0, 60))


class LayoutScene(QGraphicsScene):
    """Renders a LayoutDocument. Geometry is pulled from the document only
    when an instance is placed/edited/imported; this scene otherwise just
    holds Qt items and tracks which ones moved so the caller can commit
    final positions back into the document on mouse release."""

    port_clicked = Signal(int, str)  # inst_id, port_name — only while routing_mode is True

    # A fixed, generous virtual canvas. Without this, QGraphicsView falls
    # back to auto-computing the scene rect from itemsBoundingRect(), which
    # means the scrollable range is just the placed content's own tight
    # bounding box — if that fits inside the viewport (the common case:
    # e.g. one small waveguide in an 800x600 window), the scrollbar range
    # collapses to (0, 0) and middle-drag panning has nowhere to scroll to
    # at all. Confirmed empirically: scrollbar min/max were both exactly 0.
    # 100mm x 100mm is far larger than any realistic photonic chip (usually
    # well under 1cm) while staying small enough to not affect zoom-to-fit
    # (which fits to itemsBoundingRect(), not this rect).
    _CANVAS_HALF_EXTENT_UM = 50_000.0

    def __init__(self, document: LayoutDocument, parent=None) -> None:
        super().__init__(parent)
        self.document = document
        self.items_by_inst: dict[int, InstanceItem] = {}
        self.route_items: dict[int, InstanceItem] = {}
        self.annotation_items: dict[int, AnnotationItem] = {}
        self.reference_item: InstanceItem | None = None
        self._dirty_inst_ids: set[int] = set()
        self.routing_mode = False
        extent = self._CANVAS_HALF_EXTENT_UM
        self.setSceneRect(QRectF(-extent, -extent, 2 * extent, 2 * extent))
        self._violation_items: list[QGraphicsRectItem] = []

    def add_instance_item(self, inst_id: int) -> InstanceItem:
        item = InstanceItem(inst_id)
        polygons = self.document.get_polygons_for_instance(inst_id)
        item.set_geometry(polygons, self.document.layers)
        item.set_ports(self.document.get_ports_for_instance(inst_id))
        transform = self.document.get_transform(inst_id)
        item.apply_transform(transform.x, transform.y, transform.rotation, transform.mirror, transform.mag)
        item.rotation_deg = transform.rotation
        item.mirror = transform.mirror
        item.mag = transform.mag
        self.addItem(item)
        self.items_by_inst[inst_id] = item
        return item

    def remove_instance_item(self, inst_id: int) -> None:
        item = self.items_by_inst.pop(inst_id, None)
        if item is not None:
            self.removeItem(item)
        self._dirty_inst_ids.discard(inst_id)

    def add_route_item(self, route_id: int) -> InstanceItem:
        item = InstanceItem(route_id, movable=False)
        item.is_route = True
        shapes = self.document.get_shapes_for_route(route_id)
        item.set_geometry(shapes, self.document.layers)
        # shapes are already in absolute/top-cell coordinates, so this item
        # needs no transform of its own
        self.addItem(item)
        self.route_items[route_id] = item
        return item

    def remove_route_item(self, route_id: int) -> None:
        item = self.route_items.pop(route_id, None)
        if item is not None:
            self.removeItem(item)

    def add_annotation_item(self, ann_id: int) -> AnnotationItem:
        item = AnnotationItem(ann_id)
        item.sync_from_document(self.document)
        self.addItem(item)
        self.annotation_items[ann_id] = item
        return item

    def remove_annotation_item(self, ann_id: int) -> None:
        item = self.annotation_items.pop(ann_id, None)
        if item is not None:
            self.removeItem(item)

    def refresh_annotation_item(self, ann_id: int) -> None:
        """Re-pull a note's text/position/callouts after an edit, move, or
        callout add (mirrors resync_geometry for instances)."""
        item = self.annotation_items.get(ann_id)
        if item is not None:
            item.sync_from_document(self.document)

    def clear_annotation_items(self) -> None:
        for ann_id in list(self.annotation_items):
            self.remove_annotation_item(ann_id)

    def show_reference(self) -> None:
        self.clear_reference_item()
        item = InstanceItem(-1, movable=False, selectable=False)
        item.is_reference = True
        item.set_geometry(self.document.get_shapes_for_reference(), self.document.layers)
        self.addItem(item)
        item.setZValue(-1000)  # always render behind the user's own design
        self.reference_item = item

    def clear_reference_item(self) -> None:
        if self.reference_item is not None:
            self.removeItem(self.reference_item)
            self.reference_item = None

    def clear_all_items(self) -> None:
        for inst_id in list(self.items_by_inst):
            self.remove_instance_item(inst_id)
        for route_id in list(self.route_items):
            self.remove_route_item(route_id)
        self.clear_annotation_items()
        self.clear_reference_item()
        self.clear_drc_violations()

    def resync_geometry(self, inst_id: int) -> None:
        """Call after a property edit regenerated the instance's cell."""
        item = self.items_by_inst[inst_id]
        polygons = self.document.get_polygons_for_instance(inst_id)
        item.set_geometry(polygons, self.document.layers)
        item.set_ports(self.document.get_ports_for_instance(inst_id))

    def set_layer_visible(self, key, visible: bool) -> None:
        for item in self.items_by_inst.values():
            item.set_layer_visible(key, visible)
        for item in self.route_items.values():
            item.set_layer_visible(key, visible)

    def set_layer_color(self, key, color: str) -> None:
        for item in self.items_by_inst.values():
            item.set_layer_color(key, color)
        for item in self.route_items.values():
            item.set_layer_color(key, color)

    def mark_dirty(self, inst_id: int) -> None:
        self._dirty_inst_ids.add(inst_id)

    def dirty_instance_ids(self) -> list[int]:
        return list(self._dirty_inst_ids)

    def commit_dirty_transforms(self) -> list[int]:
        """Push moved items' final Qt positions back into the document.
        Returns the list of instance ids that were committed."""
        from phidler.model.document import Transform

        committed = list(self._dirty_inst_ids)
        for inst_id in committed:
            item = self.items_by_inst.get(inst_id)
            if item is None:
                continue
            pos = item.pos()
            self.document.set_transform(
                inst_id,
                Transform(x=pos.x(), y=pos.y(), rotation=item.rotation_deg, mirror=item.mirror, mag=item.mag),
            )
        self._dirty_inst_ids.clear()
        return committed

    def show_drc_violations(self, violations) -> None:
        self.clear_drc_violations()
        for v in violations:
            left, bottom, right, top = v.bbox
            item = QGraphicsRectItem(QRectF(left, bottom, right - left, top - bottom))
            item.setPen(_VIOLATION_PEN)
            item.setBrush(_VIOLATION_BRUSH)
            item.setZValue(1000)  # always render on top
            self.addItem(item)
            self._violation_items.append(item)

    def clear_drc_violations(self) -> None:
        for item in self._violation_items:
            self.removeItem(item)
        self._violation_items.clear()
