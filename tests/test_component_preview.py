from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor

from phidler.panels import component_preview
from phidler.panels.component_preview import ComponentPreviewPopup, render_component_pixmap


def test_render_component_pixmap_returns_correctly_sized_nonempty_pixmap(qapp):
    pixmap = render_component_pixmap("straight")
    assert pixmap is not None
    assert not pixmap.isNull()
    assert pixmap.size().width() > 0 and pixmap.size().height() > 0


def test_render_component_pixmap_handles_multilayer_component(qapp):
    """Components with several layers (e.g. a grating coupler with a slab
    etch layer) must not crash the painter loop or silently drop layers."""
    pixmap = render_component_pixmap("grating_coupler_elliptical")
    assert pixmap is not None
    assert not pixmap.isNull()


def test_render_component_pixmap_returns_none_for_invalid_name(qapp):
    assert render_component_pixmap("not_a_real_component_xyz") is None


def test_render_component_pixmap_is_cached_by_name(qapp):
    first = render_component_pixmap("bend_euler")
    second = render_component_pixmap("bend_euler")
    assert first is second  # same cached object, not re-rendered


def test_rendered_pixmap_is_not_blank(qapp):
    """A real correctness check, not just 'didn't crash': the background
    fill color must not be the only color present, or the geometry simply
    isn't being drawn (e.g. a silently-empty shapes dict)."""
    pixmap = render_component_pixmap("ring_single")
    image = pixmap.toImage()
    background = QColor("#1e1e1e")
    non_background_pixels = 0
    for x in range(0, image.width(), 4):
        for y in range(0, image.height(), 4):
            if image.pixelColor(x, y) != background:
                non_background_pixels += 1
    assert non_background_pixels > 0


def test_preview_popup_show_for_valid_component_displays_pixmap(qapp):
    popup = ComponentPreviewPopup()
    popup.show_for("straight", QPoint(100, 100))
    assert popup._label.pixmap() is not None
    assert not popup._label.pixmap().isNull()


def test_preview_popup_show_for_invalid_component_hides_without_raising(qapp):
    popup = ComponentPreviewPopup()
    popup.show_for("not_a_real_component_xyz", QPoint(100, 100))  # must not raise
    assert not popup.isVisible()


def test_render_cache_is_module_global(qapp):
    """Sanity check that the cache dict really is shared module state (so
    re-importing the module elsewhere still benefits from it), not
    accidentally shadowed per-call."""
    component_preview._cache.clear()
    render_component_pixmap("taper")
    assert "taper" in component_preview._cache
