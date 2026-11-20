"""Typed transforms apply, routes are easy to pick and are copied with their
components, and arrays can be spaced by padding."""
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from phidler.main_window import MainWindow


@pytest.fixture
def shown(qapp):
    """A shown main window, closed afterwards so it can't hold keyboard focus
    in later tests."""
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    yield win
    win.hide()  # not close(): that would ask to save the unsaved changes


def _window_with_route(win):
    doc, scene = win.document, win.scene
    a = doc.add_instance("straight", {"length": 20.0}, x=0, y=0)
    scene.add_instance_item(a.id)
    b = doc.add_instance("straight", {"length": 20.0}, x=400, y=30)
    scene.add_instance_item(b.id)
    r = doc.add_route(a.id, "o2", b.id, "o1")
    scene.add_route_item(r.id)
    return win, a.id, b.id, r.id


def _select(win, *ids):
    win.scene.clearSelection()
    for i in ids:
        item = win.scene.items_by_inst.get(i) or win.scene.route_items[i]
        item.setSelected(True)


def test_apply_transform_moves_the_component_after_focus_leaves_the_fields(shown):
    """Pressing Apply moves focus to the button; the periodic resync must not
    put the old values back before the click lands."""
    win, a, _b, _r = _window_with_route(shown)
    _select(win, a)
    QTest.qWait(300)
    panel = win.properties_panel
    panel.x_spin.setFocus()
    panel.x_spin.selectAll()
    QTest.keyClicks(panel.x_spin, "-40")
    QTest.mousePress(panel.apply_transform_button, Qt.LeftButton)
    QTest.qWait(300)  # a couple of resync ticks while the button is held
    QTest.mouseRelease(panel.apply_transform_button, Qt.LeftButton)
    assert win.document.get_transform(a).x == -40.0


def test_enter_in_a_transform_field_applies(shown):
    win, a, _b, _r = _window_with_route(shown)
    _select(win, a)
    QTest.qWait(300)
    panel = win.properties_panel
    panel.y_spin.setFocus()
    panel.y_spin.selectAll()
    QTest.keyClicks(panel.y_spin, "77")
    QTest.keyClick(panel.y_spin, Qt.Key_Return)
    assert win.document.get_transform(a).y == 77.0


def test_a_click_a_few_pixels_off_a_route_selects_it(shown):
    win, *_ids, r = _window_with_route(shown)
    view = win.view
    for px_per_um in (5.0, 0.2):
        view.resetTransform()
        view.scale(px_per_um, -px_per_um)
        view.centerOn(QPointF(200, 15))
        QTest.qWait(50)
        on_route = view.mapFromScene(QPointF(20 + 1, 0))  # just past a's output port
        for offset in (0, 3):
            win.scene.clearSelection()
            pos = on_route + type(on_route)(0, offset)
            QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
            assert win._selected_route_ids() == [r], (px_per_um, offset)


def test_duplicate_copies_the_route_between_the_selected_components(shown):
    win, a, b, r = _window_with_route(shown)
    doc = win.document
    _select(win, a, b)
    win._duplicate_selected()
    assert len(doc.instances) == 4 and len(doc.routes) == 2
    copy = next(rt for rt in doc.routes.values() if rt.id != r)
    assert {copy.instance_id_a, copy.instance_id_b}.isdisjoint({a, b})
    assert (copy.port_name_a, copy.port_name_b) == ("o2", "o1")
    # The copies (parts and route) become the selection, and it is one undo step.
    assert set(win._selected_route_ids()) == {copy.id}
    win.undo_stack.undo()
    assert len(doc.instances) == 2 and list(doc.routes) == [r]


def test_duplicating_a_route_brings_its_two_components(shown):
    win, a, b, r = _window_with_route(shown)
    _select(win, r)
    win._duplicate_selected()
    assert len(win.document.instances) == 4 and len(win.document.routes) == 2


def test_paste_keeps_layout_and_routes_and_steps_out(shown):
    win, a, b, r = _window_with_route(shown)
    doc = win.document
    doc.set_transform(b, doc.get_transform(b).__class__(x=400, y=30, rotation=90.0, mirror=False, mag=1.0))
    _select(win, a, b)
    win._copy_selected()
    win._paste_clipboard()
    win._paste_clipboard()
    assert len(doc.instances) == 6 and len(doc.routes) == 3
    step = win.view.grid_pitch * 2
    copies_of_b = sorted((t.x, t.y) for i in doc.instances if i not in (a, b)
                         for t in [doc.get_transform(i)] if t.rotation == 90.0)
    assert copies_of_b == sorted([(400 + step, 30 - step), (400 + 2 * step, 30 - 2 * step)])


def test_array_padding_sets_the_pitch_from_the_component_size(shown):
    win, a, _b, _r = _window_with_route(shown)
    _select(win, a)
    QTest.qWait(300)
    panel = win.properties_panel
    width, height = win.document.get_bbox_extent_for_instance(a)
    panel.columns_spin.setValue(3)
    assert panel.column_pitch_spin.value() == width and panel.column_padding_spin.value() == 0.0
    panel.column_padding_spin.setValue(5.0)
    assert panel.column_pitch_spin.value() == width + 5.0
    panel.row_pitch_spin.setValue(height + 2.5)
    assert panel.row_padding_spin.value() == 2.5
    panel.rows_spin.setValue(2)
    panel.apply_array_button.click()
    array = win.document.instances[a].array
    assert (array.columns, array.rows) == (3, 2)
    assert array.column_pitch == width + 5.0 and array.row_pitch == height + 2.5
