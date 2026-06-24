from PySide6.QtCore import QPointF

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.model.document import LayoutDocument, Transform


def test_zoom_to_fit_preserves_y_up_orientation(qapp):
    """fitInView could in principle replace the view's transform outright
    and drop the global Y-flip set in LayoutView.__init__, which would
    silently render the whole canvas upside down. Verified empirically
    that PySide6's fitInView composes onto the existing transform instead
    (preserves the flip) — this test locks that in as a guarantee rather
    than a one-off manual check."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 5.0, "width": 0.5})
    inst2 = doc.add_instance("straight", {"length": 5.0, "width": 0.5})
    doc.set_transform(inst2.id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))

    scene = LayoutScene(doc)
    for inst_id in doc.instances:
        scene.add_instance_item(inst_id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    # a point with a high scene-y ("up" in GDS/Y-up terms) must map to a
    # SMALLER viewport pixel-y than a point with a low scene-y, both
    # before and after zoom_to_fit
    high_pt = QPointF(0.0, 19.0)
    low_pt = QPointF(0.0, 1.0)

    assert view.mapFromScene(high_pt).y() < view.mapFromScene(low_pt).y()

    view.zoom_to_fit()

    assert view.mapFromScene(high_pt).y() < view.mapFromScene(low_pt).y()
    # the global flip must still be the only sign-flip present
    assert view.transform().m22() < 0


def test_zoom_to_fit_brings_distant_content_into_view(qapp):
    """A fresh QGraphicsView auto-centers/clamps scrolling to wherever its
    content is, which makes "is the lone item visible before fitting"
    unreliable to set up (confirmed empirically: neither placing it far
    from the origin nor calling centerOn(0,0) first escapes that — Qt
    clamps the scrollable range to the content's own vicinity). So this
    tests the property that's actually load-bearing and isn't subject to
    that: with two items far apart, only zoom_to_fit's explicit scale-down
    brings *both* into view at once; the default 1:1 scale physically
    cannot fit a 5000-unit span into a 400px viewport."""
    doc = LayoutDocument()
    near = doc.add_instance("straight", {"length": 5.0, "width": 0.5}, x=0.0, y=0.0)
    far = doc.add_instance("straight", {"length": 5.0, "width": 0.5}, x=5000.0, y=5000.0)
    scene = LayoutScene(doc)
    near_item = scene.add_instance_item(near.id)
    far_item = scene.add_instance_item(far.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    scale_before = abs(view.transform().m11())
    view.zoom_to_fit()
    scale_after = abs(view.transform().m11())

    assert scale_after < scale_before  # had to zoom out to fit a 5000-unit span

    visible_after = view.mapToScene(view.viewport().rect()).boundingRect()
    assert visible_after.contains(near_item.sceneBoundingRect())
    assert visible_after.contains(far_item.sceneBoundingRect())


def test_zoom_to_selection_uses_only_selected_items(qapp):
    doc = LayoutDocument()
    inst_a = doc.add_instance("straight", {"length": 5.0, "width": 0.5}, x=0.0, y=0.0)
    inst_b = doc.add_instance("straight", {"length": 5.0, "width": 0.5}, x=1000.0, y=1000.0)
    scene = LayoutScene(doc)
    item_a = scene.add_instance_item(inst_a.id)
    item_b = scene.add_instance_item(inst_b.id)

    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    item_a.setSelected(True)
    view.zoom_to_selection()

    visible = view.mapToScene(view.viewport().rect()).boundingRect()
    assert visible.intersects(item_a.sceneBoundingRect())
    assert not visible.intersects(item_b.sceneBoundingRect())


def test_zoom_to_fit_on_empty_scene_does_not_raise(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()
    view.zoom_to_fit()  # must not raise on an empty scene


def test_zoom_to_selection_with_nothing_selected_does_not_raise(qapp):
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 5.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(inst.id)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()
    view.zoom_to_selection()  # must not raise when selection is empty
