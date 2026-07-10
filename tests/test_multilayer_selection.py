"""Selecting items that overlap (stacked on top of each other): the
selectable_items_at stack, Alt+click cycling, and the right-click picker menu.
"""

from __future__ import annotations

from phidler.main_window import MainWindow


def _overlapping_pair(win):
    """Place two straights at the same spot so they overlap, and return
    (top_item, bottom_item, viewport_point_over_both)."""
    a = win.document.add_instance("straight", {"length": 10.0}, x=0.0, y=0.0)
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("straight", {"length": 10.0}, x=0.0, y=0.0)
    win.scene.add_instance_item(b.id)

    item_a = win.scene.items_by_inst[a.id]
    item_b = win.scene.items_by_inst[b.id]  # added last -> stacked on top
    center = item_b.mapToScene(item_b.boundingRect().center())
    win.view.centerOn(center)
    vp = win.view.mapFromScene(center)
    return item_b, item_a, vp, (a.id, b.id)


def test_selectable_items_at_returns_overlapping_stack_top_first(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    top, bottom, vp, _ids = _overlapping_pair(win)

    stack = win.view.selectable_items_at(vp)
    assert stack == [top, bottom]  # top-most first


def test_selectable_items_at_excludes_reference_and_empty(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    # A point far from anything is an empty stack.
    from PySide6.QtCore import QPoint

    assert win.view.selectable_items_at(QPoint(5, 5)) == [] or all(
        getattr(i, "inst_id", None) in win.document.instances
        or getattr(i, "inst_id", None) in win.document.routes
        for i in win.view.selectable_items_at(QPoint(5, 5))
    )


def test_alt_click_cycles_down_and_wraps(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    top, bottom, vp, _ids = _overlapping_pair(win)

    # Nothing selected yet -> first cycle picks the top-most.
    assert win.view.cycle_select_at(vp) is top
    assert win.scene.selectedItems() == [top]

    # Next cycle steps to the one underneath.
    assert win.view.cycle_select_at(vp) is bottom
    assert win.scene.selectedItems() == [bottom]

    # And wraps back to the top.
    assert win.view.cycle_select_at(vp) is top
    assert win.scene.selectedItems() == [top]


def test_cycle_on_empty_clears_selection(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    top, _bottom, vp, _ids = _overlapping_pair(win)
    top.setSelected(True)

    from PySide6.QtCore import QPoint

    assert win.view.cycle_select_at(QPoint(3, 3)) is None
    assert win.scene.selectedItems() == []


def test_context_menu_has_select_under_cursor_for_a_stack(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    top, bottom, vp, (a_id, b_id) = _overlapping_pair(win)

    menu = win._build_canvas_context_menu(vp)
    submenus = [a.menu() for a in menu.actions() if a.menu() is not None and a.text() == "Select under cursor"]
    assert len(submenus) == 1
    entries = submenus[0].actions()
    assert len(entries) == 2  # one per overlapping item
    # Labels name the component and disambiguate by id.
    assert f"#{b_id}" in entries[0].text() and "straight" in entries[0].text()

    # Triggering the first entry (the top item) selects exactly it.
    entries[0].trigger()
    assert win.scene.selectedItems() == [top]
    # Triggering the second selects the buried one.
    entries[1].trigger()
    assert win.scene.selectedItems() == [bottom]


def test_context_menu_omits_picker_for_a_single_item(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    inst = win.document.add_instance("straight", {"length": 10.0}, x=0.0, y=0.0)
    win.scene.add_instance_item(inst.id)
    item = win.scene.items_by_inst[inst.id]
    center = item.mapToScene(item.boundingRect().center())
    win.view.centerOn(center)
    vp = win.view.mapFromScene(center)

    menu = win._build_canvas_context_menu(vp)
    assert not any(a.text() == "Select under cursor" for a in menu.actions())
    # And a menu built with no position never has it.
    assert not any(a.text() == "Select under cursor" for a in win._build_canvas_context_menu().actions())


def test_item_display_label_covers_instance_and_route(qapp):
    win = MainWindow()
    a = win.document.add_instance("straight", {"length": 10.0}, x=0.0, y=0.0)
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("straight", {"length": 10.0}, x=20.0, y=0.0, rotation=180.0)
    win.scene.add_instance_item(b.id)
    route = win.document.add_route(a.id, "o2", b.id, "o2", "strip")
    win.scene.add_route_item(route.id)

    inst_label = win._item_display_label(win.scene.items_by_inst[a.id])
    assert "straight" in inst_label and f"#{a.id}" in inst_label
    route_label = win._item_display_label(win.scene.route_items[route.id])
    assert "route" in route_label and "strip" in route_label
