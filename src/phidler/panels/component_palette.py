from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from phidler.pdk_catalog import (
    CORE_CATEGORIES,
    CUSTOM_CATEGORY,
    ComponentSpec,
    category_display_name,
    prettify_component_name,
)

from .component_preview import ComponentPreviewPopup

_PREVIEW_OFFSET = QPoint(24, 12)

_NAME_ROLE = Qt.UserRole
_OTHER_LABEL = "Other"


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
        self.tree.viewport().installEventFilter(self)
        layout.addWidget(self.tree)

        self._preview_popup = ComponentPreviewPopup()

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

    def eventFilter(self, obj, event) -> bool:
        if obj is self.tree.viewport() and event.type() == QEvent.Leave:
            self._preview_popup.hide()
        return super().eventFilter(obj, event)
