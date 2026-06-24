import gdsfactory as gf

from phidler.main_window import MainWindow
from phidler.pdk_catalog import build_catalog


def test_place_over_100_varied_components_renders_and_exports(qapp, tmp_path):
    """Earlier manual smoke testing covered ~10 diverse components placed by
    hand. This is the permanent, larger-scale version: walk the catalog,
    place 100+ distinct real components across every category (not just
    repeats of 'straight'), and confirm the whole pipeline — placement,
    rendering, GDS export, reimport — holds up at that scale."""
    win = MainWindow()
    win.view.resize(800, 600)
    win.view.show()

    catalog = build_catalog()
    flat = [spec for specs in catalog.values() for spec in specs]
    assert len(flat) > 100

    placed = []
    columns = 12
    spacing = 80.0
    for i, spec in enumerate(flat[:120]):
        x = (i % columns) * spacing
        y = (i // columns) * spacing
        inst = win.document.add_instance(spec.name, {}, x=x, y=y)
        win.scene.add_instance_item(inst.id)
        placed.append((spec.name, inst.id))

    assert len(placed) == 120
    assert len(win.document.instances) == 120
    assert len(win.scene.items_by_inst) == 120

    out = tmp_path / "scale.gds"
    win.document.export_gds(str(out))

    reimported = gf.import_gds(str(out))
    assert len(list(reimported.insts)) == 120
    assert not reimported.bbox().empty()


def test_select_all_and_delete_at_scale_is_undoable(qapp):
    """Exercises the undo macro machinery with a larger batch than any
    other test — delete-all-then-undo is exactly the kind of bulk edit a
    real layout session does after a mistake."""
    win = MainWindow()
    catalog = build_catalog()
    flat = [spec for specs in catalog.values() for spec in specs][:50]
    for i, spec in enumerate(flat):
        inst = win.document.add_instance(spec.name, {}, x=i * 50.0, y=0.0)
        win.scene.add_instance_item(inst.id)

    assert len(win.document.instances) == 50

    win._select_all()
    win._delete_selected()
    assert len(win.document.instances) == 0
    assert len(win.scene.items_by_inst) == 0

    win.undo_stack.undo()
    assert len(win.document.instances) == 50
    assert len(win.scene.items_by_inst) == 50


def test_entire_catalog_is_placeable(qapp):
    """The 120-sample test above is fast and representative, but it already
    caught two distinct classes of broken catalog entries that a smaller
    hand-picked sample missed (unregistered PDK names, ComponentAllAngle
    factories needing a different placement API). This is the exhaustive
    version — every single cataloged component, not a sample — as a
    permanent regression guard on build_catalog()'s filtering logic."""
    win = MainWindow()
    catalog = build_catalog()
    flat = [spec for specs in catalog.values() for spec in specs]
    assert len(flat) > 250

    failures = []
    for i, spec in enumerate(flat):
        try:
            inst = win.document.add_instance(spec.name, {}, x=i * 50.0, y=0.0)
            win.scene.add_instance_item(inst.id)
        except Exception as exc:
            failures.append((spec.name, type(exc).__name__, str(exc)[:200]))

    assert failures == []
    assert len(win.document.instances) == len(flat)
