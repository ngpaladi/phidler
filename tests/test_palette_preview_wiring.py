from PySide6.QtCore import QEvent

from phidler.pdk_catalog import build_catalog
from phidler.panels.component_palette import ComponentPalette


def _find_top_level(palette, label_prefix: str):
    for i in range(palette.tree.topLevelItemCount()):
        item = palette.tree.topLevelItem(i)
        if item.text(0).startswith(label_prefix):
            return item
    return None


def test_hovering_a_leaf_item_shows_the_preview_popup(qapp):
    """itemEntered is a plain Qt signal — emitting it directly (rather than
    injecting a synthetic native mouse-move event) exercises the same
    connected slot without the event-injection instability seen elsewhere
    in this codebase (a QContextMenuEvent via QApplication.sendEvent
    segfaulted the interpreter under this offscreen platform)."""
    catalog = build_catalog()
    palette = ComponentPalette(catalog)
    palette.show()

    waveguides_item = _find_top_level(palette, "Waveguides")
    children = [waveguides_item.child(i) for i in range(waveguides_item.childCount())]
    straight_item = next(c for c in children if c.text(0) == "Straight")

    assert not palette._preview_popup.isVisible()
    palette.tree.itemEntered.emit(straight_item, 0)
    assert palette._preview_popup.isVisible()


def test_hovering_a_category_header_hides_the_preview(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)
    palette.show()

    waveguides_item = _find_top_level(palette, "Waveguides")
    children = [waveguides_item.child(i) for i in range(waveguides_item.childCount())]
    straight_item = next(c for c in children if c.text(0) == "Straight")
    palette.tree.itemEntered.emit(straight_item, 0)
    assert palette._preview_popup.isVisible()

    palette.tree.itemEntered.emit(waveguides_item, 0)  # category header, not a leaf
    assert not palette._preview_popup.isVisible()


def test_mouse_leaving_the_tree_hides_the_preview(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)
    palette.show()

    waveguides_item = _find_top_level(palette, "Waveguides")
    children = [waveguides_item.child(i) for i in range(waveguides_item.childCount())]
    straight_item = next(c for c in children if c.text(0) == "Straight")
    palette.tree.itemEntered.emit(straight_item, 0)
    assert palette._preview_popup.isVisible()

    palette.eventFilter(palette.tree.viewport(), QEvent(QEvent.Leave))
    assert not palette._preview_popup.isVisible()


def test_activating_an_item_hides_the_preview(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)
    palette.show()

    waveguides_item = _find_top_level(palette, "Waveguides")
    children = [waveguides_item.child(i) for i in range(waveguides_item.childCount())]
    straight_item = next(c for c in children if c.text(0) == "Straight")
    palette.tree.itemEntered.emit(straight_item, 0)
    assert palette._preview_popup.isVisible()

    received = []
    palette.place_requested.connect(received.append)
    palette.tree.itemActivated.emit(straight_item, 0)

    assert not palette._preview_popup.isVisible()
    assert received == ["straight"]


def test_filtering_hides_the_preview(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)
    palette.show()

    waveguides_item = _find_top_level(palette, "Waveguides")
    children = [waveguides_item.child(i) for i in range(waveguides_item.childCount())]
    straight_item = next(c for c in children if c.text(0) == "Straight")
    palette.tree.itemEntered.emit(straight_item, 0)
    assert palette._preview_popup.isVisible()

    palette.search_box.setText("mmi")
    assert not palette._preview_popup.isVisible()
