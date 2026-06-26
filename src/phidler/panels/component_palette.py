from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from phidler.pdk_catalog import (
    CORE_CATEGORIES,
    CUSTOM_CATEGORY,
    ComponentSpec,
    category_display_name,
    prettify_component_name,
)

from .component_preview import (
    ComponentPreviewPopup,
    cached_pixmap,
    has_cached_pixmap,
    render_component_pixmap,
)

_PREVIEW_OFFSET = QPoint(24, 12)

_NAME_ROLE = Qt.UserRole
_OTHER_LABEL = "Other"

# Inline thumbnail icon size in the tree. Keeps the 180×140 preview aspect
# ratio so a downscaled component reads correctly next to its name.
_THUMBNAIL_SIZE = QSize(52, 40)


class ComponentPalette(QWidget):
    """Category tree of placeable gdsfactory components. Double-click (or
    Enter) arms placement mode; the canvas then places the component on the
    next click (see LayoutView.armed_component).

    Core photonics categories (waveguides, bends, couplers, MMIs, rings,
    etc.) are shown expanded at the top level; the generic PDK's other
    domains (MEMS, quantum/superconducting electronics, microfluidics,
    analog RF, process-control-monitor test structures, ...) are grouped
    under one collapsed "Other" node so they're available but out of the
    way — this app is for photonic circuits first."""

    place_requested = Signal(str)

    def __init__(self, catalog: dict[str, list[ComponentSpec]], parent=None) -> None:
        super().__init__(parent)
        self._catalog: dict[str, list[ComponentSpec]] = {}

        layout = QVBoxLayout(self)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter components…")
        self.search_box.textChanged.connect(self._populate)
        layout.addWidget(self.search_box)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMouseTracking(True)  # required for itemEntered to fire on hover
        # A single click already arms placement (itemClicked) — itemActivated
        # (double-click or Enter) is also wired to the same handler so both
        # still work, but a single click used to be a no-op here, requiring
        # an extra double-click just to start placing a component. That's
        # not how palette-driven placement works in other CAD/drawing tools
        # (click the tool, then click the canvas) — reported as unintuitive,
        # and confirmed it really was an unnecessary extra click.
        self.tree.itemClicked.connect(self._on_item_activated)
        self.tree.itemActivated.connect(self._on_item_activated)
        self.tree.itemEntered.connect(self._on_item_entered)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.viewport().installEventFilter(self)
        self.tree.setIconSize(_THUMBNAIL_SIZE)
        layout.addWidget(self.tree)

        self._preview_popup = ComponentPreviewPopup()

        # Inline thumbnails are on by default but rendered lazily: each
        # component costs ~40ms to rasterize from gdsfactory and the catalog
        # runs to hundreds, so eager rendering would freeze startup for
        # seconds. Instead only items in an expanded category are queued, and
        # a 0-interval timer drains the queue one at a time so the UI stays
        # responsive while thumbnails fill in. render_component_pixmap caches
        # per name, so re-populating (e.g. while filtering) reuses results.
        self._thumbnails_visible = True
        self._thumb_queue: list[tuple[QTreeWidgetItem, str, int]] = []
        self._populate_gen = 0  # bumped each _populate; stale queue entries are dropped
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(0)
        self._thumb_timer.timeout.connect(self._render_next_thumbnail)

        self.add_components(catalog)

    def add_components(self, specs_by_category: dict[str, list[ComponentSpec]]) -> None:
        """Merges additional components into the palette (used both for the
        initial built-in catalog and for components loaded later via File >
        Import Custom Components) and re-renders the tree."""
        for category, specs in specs_by_category.items():
            self._catalog.setdefault(category, []).extend(specs)
        self._populate(self.search_box.text())

    def _populate(self, filter_text: str) -> None:
        self._preview_popup.hide()  # about-to-be-cleared items can't stay previewed
        # tree.clear() deletes the items any queued thumbnails point at, so
        # drop the queue and bump the generation: enqueues during this rebuild
        # (fired by setExpanded below) belong to the new generation only.
        self._populate_gen += 1
        self._thumb_queue.clear()
        self.tree.clear()
        needle = filter_text.lower().strip()

        other_categories = sorted(c for c in self._catalog if c not in CORE_CATEGORIES and c != CUSTOM_CATEGORY)
        ordered_top_level = [c for c in CORE_CATEGORIES if c in self._catalog]
        if CUSTOM_CATEGORY in self._catalog:
            ordered_top_level.append(CUSTOM_CATEGORY)

        for category in ordered_top_level:
            self._add_category_item(self.tree, category, needle, expand=not needle)

        if other_categories:
            other_item = QTreeWidgetItem([_OTHER_LABEL])
            self.tree.addTopLevelItem(other_item)
            any_match = False
            for category in other_categories:
                if self._add_category_item(other_item, category, needle, expand=bool(needle)):
                    any_match = True
            if not any_match and needle:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(other_item))
            other_item.setExpanded(bool(needle))

    def _add_category_item(self, parent, category: str, needle: str, expand: bool) -> bool:
        """Adds `category`'s matching components as a child tree item under
        `parent`. Returns whether anything matched (so callers can decide
        whether the category/Other node is worth keeping while filtering)."""
        specs = self._catalog.get(category, [])
        matching = [
            s
            for s in specs
            if not needle or needle in s.name.lower() or needle in prettify_component_name(s.name).lower()
        ]
        if not matching:
            return False
        cat_item = QTreeWidgetItem([f"{category_display_name(category)} ({len(matching)})"])
        if isinstance(parent, QTreeWidget):
            parent.addTopLevelItem(cat_item)
        else:
            parent.addChild(cat_item)
        for spec in sorted(matching, key=lambda s: s.name):
            pretty = prettify_component_name(spec.name)
            child = QTreeWidgetItem([pretty])
            child.setToolTip(0, spec.name)
            child.setData(0, _NAME_ROLE, spec.name)
            cat_item.addChild(child)
        cat_item.setExpanded(expand)
        return True

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        self._preview_popup.hide()
        name = item.data(0, _NAME_ROLE)
        if name:
            self.place_requested.emit(name)

    def _on_item_entered(self, item: QTreeWidgetItem, _column: int) -> None:
        name = item.data(0, _NAME_ROLE)
        if name:
            self._preview_popup.show_for(name, QCursor.pos() + _PREVIEW_OFFSET)
        else:
            self._preview_popup.hide()  # hovering a category/"Other" node, not a placeable leaf

    # -- inline thumbnails ----------------------------------------------------

    def set_thumbnails_visible(self, visible: bool) -> None:
        """Show or hide the inline component thumbnail icons (the View menu's
        'Component Thumbnails' toggle). Rebuilds the tree so icons are either
        attached (lazily, per expanded category) or absent."""
        if visible == self._thumbnails_visible:
            return
        self._thumbnails_visible = visible
        # Icon size drives row height for the whole tree, so clear it when off
        # to get compact text-only rows back (and restore it when on).
        self.tree.setIconSize(_THUMBNAIL_SIZE if visible else QSize())
        self._populate(self.search_box.text())

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        """A category was expanded (including programmatically during
        _populate) — queue thumbnails for the component leaves it just
        revealed. Category/'Other' nodes have no name and are skipped."""
        if not self._thumbnails_visible:
            return
        for i in range(item.childCount()):
            child = item.child(i)
            name = child.data(0, _NAME_ROLE)
            if name:
                self._enqueue_thumbnail(child, name)

    def _enqueue_thumbnail(self, item: QTreeWidgetItem, name: str) -> None:
        # Already-rendered thumbnails are set immediately (cheap); the rest
        # are deferred to the background timer so populate never blocks.
        if has_cached_pixmap(name):
            pixmap = cached_pixmap(name)
            if pixmap is not None:
                item.setIcon(0, QIcon(pixmap))
            return
        self._thumb_queue.append((item, name, self._populate_gen))
        # The offscreen platform is the headless test environment; auto-running
        # the timer there would paint hundreds of pixmaps during any test that
        # spins the event loop, for no visual benefit. Tests drive
        # _render_next_thumbnail directly instead.
        if not self._thumb_timer.isActive() and QApplication.platformName() != "offscreen":
            self._thumb_timer.start()

    def _render_next_thumbnail(self) -> None:
        while self._thumb_queue:
            item, name, gen = self._thumb_queue.pop(0)
            if not self._thumbnails_visible:
                self._thumb_queue.clear()
                break
            if gen != self._populate_gen:
                continue  # item belonged to a since-rebuilt tree
            pixmap = render_component_pixmap(name)
            if pixmap is not None:
                try:
                    item.setIcon(0, QIcon(pixmap))
                except RuntimeError:
                    continue  # underlying C++ item was deleted; skip it
            return  # one per tick — yield to the event loop so the UI stays live
        self._thumb_timer.stop()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.tree.viewport() and event.type() == QEvent.Leave:
            self._preview_popup.hide()
        return super().eventFilter(obj, event)
