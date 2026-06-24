import inspect

from PySide6.QtCore import Qt

from phidler.panels.component_palette import ComponentPalette
from phidler.pdk_catalog import ComponentSpec, build_catalog


def _top_level_labels(palette: ComponentPalette) -> list[str]:
    return [palette.tree.topLevelItem(i).text(0) for i in range(palette.tree.topLevelItemCount())]


def _find_top_level(palette: ComponentPalette, label_prefix: str):
    for i in range(palette.tree.topLevelItemCount()):
        item = palette.tree.topLevelItem(i)
        if item.text(0).startswith(label_prefix):
            return item
    return None


def test_core_categories_are_top_level_and_expanded(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)

    labels = _top_level_labels(palette)
    assert any(label.startswith("Waveguides") for label in labels)
    assert any(label.startswith("MMIs") for label in labels)

    waveguides_item = _find_top_level(palette, "Waveguides")
    assert waveguides_item is not None
    assert waveguides_item.isExpanded()


def test_niche_categories_are_nested_under_other_and_collapsed(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)

    labels = _top_level_labels(palette)
    assert "Other" in labels
    # niche categories must NOT appear as their own top-level items
    assert not any(label.startswith("MEMS") for label in labels)
    assert not any(label.startswith("Quantum") for label in labels)

    other_item = _find_top_level(palette, "Other")
    assert not other_item.isExpanded()
    child_labels = [other_item.child(i).text(0) for i in range(other_item.childCount())]
    assert any(label.startswith("MEMS") for label in child_labels)
    assert any(label.startswith("Quantum") for label in child_labels)


def test_leaf_items_show_pretty_name_with_raw_name_as_tooltip_and_data(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)

    waveguides_item = _find_top_level(palette, "Waveguides")
    children = [waveguides_item.child(i) for i in range(waveguides_item.childCount())]
    straight_item = next(c for c in children if c.text(0) == "Straight")
    assert straight_item.toolTip(0) == "straight"
    assert straight_item.data(0, Qt.UserRole) == "straight"


def test_filter_matches_both_raw_and_pretty_names(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)

    palette.search_box.setText("mmi")
    # filtering should find "mmi1x2" by raw name and show it under a
    # (possibly renamed/expanded) category — count tree leaves directly
    found = []

    def collect(item):
        for i in range(item.childCount()):
            child = item.child(i)
            if child.childCount() == 0:
                found.append(child.text(0))
            collect(child)

    for i in range(palette.tree.topLevelItemCount()):
        collect(palette.tree.topLevelItem(i))

    assert any("MMI" in label for label in found)


def test_add_components_merges_custom_category(qapp):
    catalog = build_catalog()
    palette = ComponentPalette(catalog)
    assert _find_top_level(palette, "Custom") is None

    def fake_factory(length: float = 1.0):
        return None

    spec = ComponentSpec(name="my_part", category="custom", factory=fake_factory, signature=inspect.signature(fake_factory))
    palette.add_components({"custom": [spec]})

    custom_item = _find_top_level(palette, "Custom")
    assert custom_item is not None
    child_labels = [custom_item.child(i).text(0) for i in range(custom_item.childCount())]
    assert "My Part" in child_labels
