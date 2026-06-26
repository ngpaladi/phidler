"""Inline component thumbnails in the palette: lazy, per-expanded-category
rendering driven by a background timer, toggled from the View menu."""

import phidler.panels.component_preview as preview
from phidler.pdk_catalog import build_catalog
from phidler.panels.component_palette import ComponentPalette


def _find_top_level(palette, label_prefix: str):
    for i in range(palette.tree.topLevelItemCount()):
        item = palette.tree.topLevelItem(i)
        if item.text(0).startswith(label_prefix):
            return item
    return None


def _leaves(item):
    return [item.child(i) for i in range(item.childCount())]


def _drain(palette):
    # Mimic the background timer, which doesn't fire under the offscreen
    # platform — render every queued thumbnail synchronously.
    guard = 0
    while palette._thumb_queue:
        palette._render_next_thumbnail()
        guard += 1
        assert guard < 10_000  # the queue must shrink, never spin forever


def _small_catalog():
    """A trimmed real catalog: a couple of waveguides (a core category, so
    expanded by default) plus one non-core category (lands under 'Other',
    collapsed). Real ComponentSpecs so rendering works, but few enough that
    clearing the cache and re-rendering per test stays fast."""
    full = build_catalog()
    from phidler.pdk_catalog import CORE_CATEGORIES

    waveguides = full["waveguides"]
    wg = [s for s in waveguides if s.name == "straight"][:1] + [
        s for s in waveguides if s.name != "straight"
    ][:1]

    other_cat = next(c for c in full if c not in CORE_CATEGORIES and c != "custom")
    return {"waveguides": wg, other_cat: full[other_cat][:2]}


def _fresh_palette(qapp):
    # The preview cache is process-global and persists across tests; clear it
    # so queue/timer assertions don't depend on what earlier tests rendered.
    preview._cache.clear()
    palette = ComponentPalette(_small_catalog())
    palette.show()
    return palette


def test_expanded_category_leaves_get_thumbnails_after_the_queue_drains(qapp):
    palette = _fresh_palette(qapp)
    waveguides = _find_top_level(palette, "Waveguides")  # a core category, expanded by default
    assert waveguides.isExpanded()

    # Cold cache + expanded category -> its leaves are queued, not yet drawn.
    leaves = _leaves(waveguides)
    assert palette._thumb_queue, "expanded category should queue thumbnails"
    assert all(leaf.icon(0).isNull() for leaf in leaves)

    _drain(palette)
    assert all(not leaf.icon(0).isNull() for leaf in leaves)


def test_background_timer_does_not_auto_start_under_offscreen(qapp):
    palette = _fresh_palette(qapp)
    # Queue has work (cold cache) but the timer stays idle headlessly — tests
    # drive _render_next_thumbnail directly instead of spinning the loop.
    assert palette._thumb_queue
    assert not palette._thumb_timer.isActive()


def test_collapsed_other_categories_do_not_render_until_expanded(qapp):
    palette = _fresh_palette(qapp)
    other = _find_top_level(palette, "Other")
    assert other is not None and not other.isExpanded()

    category = other.child(0)  # a category node under Other, collapsed
    assert not category.isExpanded()
    _drain(palette)
    # Nothing under the collapsed Other branch should have been rendered.
    assert all(leaf.icon(0).isNull() for leaf in _leaves(category))

    category.setExpanded(True)  # fires itemExpanded -> queues this category's leaves
    _drain(palette)
    assert all(not leaf.icon(0).isNull() for leaf in _leaves(category))


def test_toggling_thumbnails_off_then_on(qapp):
    palette = _fresh_palette(qapp)
    _drain(palette)
    waveguides = _find_top_level(palette, "Waveguides")
    assert all(not leaf.icon(0).isNull() for leaf in _leaves(waveguides))

    palette.set_thumbnails_visible(False)
    # Tree was rebuilt without icons and nothing is queued.
    assert not palette._thumb_queue
    waveguides = _find_top_level(palette, "Waveguides")
    assert all(leaf.icon(0).isNull() for leaf in _leaves(waveguides))

    palette.set_thumbnails_visible(True)
    _drain(palette)
    waveguides = _find_top_level(palette, "Waveguides")
    assert all(not leaf.icon(0).isNull() for leaf in _leaves(waveguides))


def test_repopulating_drops_stale_queue_entries(qapp):
    palette = _fresh_palette(qapp)
    stale_item, stale_name, stale_gen = palette._thumb_queue[0]
    assert stale_gen == palette._populate_gen

    # Filtering rebuilds the tree (and deletes the old items); the old queue
    # entry is now a different generation and must be skipped, not drawn onto
    # a deleted C++ object.
    palette.search_box.setText("straight")
    assert palette._populate_gen != stale_gen

    palette._thumb_queue.insert(0, (stale_item, stale_name, stale_gen))
    palette._render_next_thumbnail()  # must not raise on the deleted stale item
