"""Routes avoid crossing other placed components: when a component sits on the
straight path, the route detours around it (best-effort, verified — falls back
to a direct route when no single detour can clear everything)."""

import klayout.db as kdb

from phidler.model.document import LayoutDocument


def _route_crosses_instance(doc, route, inst_id):
    bb = doc.instances[inst_id].ref.dbbox()
    dbu = doc.top.kcl.dbu
    region = kdb.Region()
    for ref in route.refs:
        for shapes in doc._shapes_for_ref(ref).values():
            for hull, _holes in shapes:
                region.insert(kdb.Polygon([kdb.Point(round(x / dbu), round(y / dbu)) for x, y in hull]))
    ibox = kdb.Box(round(bb.left / dbu), round(bb.bottom / dbu), round(bb.right / dbu), round(bb.top / dbu))
    return not (region & kdb.Region(ibox)).is_empty()


def _len_um(doc, route):
    return route.length * doc.top.kcl.dbu


def test_route_detours_around_a_blocking_component(qapp):
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=200.0, y=0.0)
    blocker = doc.add_instance("mmi1x2", {}, x=95.0, y=0.0)  # squarely on the straight path

    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    assert not _route_crosses_instance(doc, route, blocker.id)  # routed around it


def test_clear_path_is_not_needlessly_detoured(qapp):
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=200.0, y=0.0)
    doc.add_instance("mmi1x2", {}, x=95.0, y=60.0)  # well off the route line

    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    # ~190µm direct; a detour would add tens of µm. Stays near-direct.
    assert _len_um(doc, route) < 210.0


def test_route_is_always_created_even_when_avoidance_cant_clear(qapp):
    """When no single detour clears everything (components above, below, and on
    the line), the route falls back to a plain direct route — it must still be
    created, never raise."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=200.0, y=0.0)
    doc.add_instance("mmi1x2", {}, x=95.0, y=0.0)
    doc.add_instance("mmi1x2", {}, x=95.0, y=25.0)
    doc.add_instance("mmi1x2", {}, x=95.0, y=-25.0)

    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    assert route.id in doc.routes  # created (direct fallback), no exception
