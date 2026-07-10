"""Altering a route's length goal after it's placed: the document re-route,
the undoable command, and the Properties-panel route editor."""

from __future__ import annotations

from phidler.canvas.scene import LayoutScene
from phidler.model.commands import SetRouteLengthCommand
from phidler.model.document import LayoutDocument
from phidler.main_window import MainWindow
from phidler.panels.properties_panel import PropertiesPanel


def _two_straights(doc, gap_x=100.0):
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=gap_x, y=0.0)
    return a, b


def _len_um(doc, route):
    return route.length * doc.top.kcl.dbu


# -- document.set_route_goal ---------------------------------------------------


def test_set_route_goal_meanders_a_plain_route_to_a_new_length(qapp):
    doc = LayoutDocument()
    a, b = _two_straights(doc)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")  # no goal
    assert route.goal_length_um is None
    natural = _len_um(doc, route)

    rebuilt = doc.set_route_goal(route.id, 200.0, auto_match=True)
    assert rebuilt.id == route.id  # same id, edited in place
    assert rebuilt.goal_length_um == 200.0
    assert rebuilt.meander_amplitude_um is not None  # a meander was inserted
    assert _len_um(doc, rebuilt) > natural + 50  # visibly longer
    assert abs(_len_um(doc, rebuilt) - 200.0) < 2.0  # close to the target


def test_set_route_goal_none_clears_the_meander(qapp):
    doc = LayoutDocument()
    a, b = _two_straights(doc)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", goal_length_um=200.0, auto_match=True)
    long_len = _len_um(doc, route)

    cleared = doc.set_route_goal(route.id, None, auto_match=False)
    assert cleared.goal_length_um is None
    assert cleared.meander_amplitude_um is None
    assert _len_um(doc, cleared) < long_len  # back to the direct route


# -- SetRouteLengthCommand (undo/redo) ----------------------------------------


def test_set_route_length_command_is_undoable(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a, b = _two_straights(doc)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    scene.add_route_item(route.id)
    natural = _len_um(doc, route)

    cmd = SetRouteLengthCommand(doc, scene, route.id, None, False, 200.0, True)
    cmd.redo()
    assert cmd.error is None
    assert doc.routes[route.id].goal_length_um == 200.0
    assert _len_um(doc, doc.routes[route.id]) > natural + 50
    assert route.id in scene.route_items  # scene item rebuilt

    cmd.undo()
    assert doc.routes[route.id].goal_length_um is None
    assert abs(_len_um(doc, doc.routes[route.id]) - natural) < 1.0
    assert route.id in scene.route_items


# -- Properties panel route editor --------------------------------------------


def test_properties_panel_show_route_toggles_to_route_mode(qapp):
    panel = PropertiesPanel()
    panel.show_route(7, length_um=42.0, goal_um=None, auto=True, time_str="0.5 ps")
    # isHidden(), not isVisible(): a child widget's isVisible() is False while the
    # top-level window is unshown (as under offscreen), but isHidden() reflects
    # the explicit setVisible flag we set.
    assert not panel.route_group.isHidden()
    assert panel.transform_group.isHidden()
    assert panel.array_group.isHidden()
    assert "42.000" in panel.route_length_label.text()

    # Selecting an instance switches back to instance mode.
    import inspect

    def straight(length: float = 10.0):
        pass

    panel.show_instance(3, "straight", inspect.signature(straight), {"length": 10.0})
    assert panel.route_group.isHidden()
    assert not panel.transform_group.isHidden()


def test_properties_panel_route_length_applied_signal(qapp):
    panel = PropertiesPanel()
    panel.show_route(9, length_um=42.0, goal_um=100.0, auto=True, time_str="")
    received = []
    panel.route_length_applied.connect(lambda *a: received.append(a))

    panel.route_target_spin.setValue(250.0)
    panel.route_target_unit.setCurrentText("µm")
    panel.route_auto_check.setChecked(True)
    panel._on_apply_route_length()
    assert received == [(9, 250.0, "µm", True)]


# -- MainWindow integration ----------------------------------------------------


def test_selecting_a_route_shows_the_length_editor(qapp):
    win = MainWindow()
    a, b = _two_straights(win.document)
    win.scene.add_instance_item(a.id)
    win.scene.add_instance_item(b.id)
    route = win.document.add_route(a.id, "o2", b.id, "o1", "strip")
    win.scene.add_route_item(route.id)

    win.scene.route_items[route.id].setSelected(True)
    win._on_selection_changed()
    assert not win.properties_panel.route_group.isHidden()
    assert win.properties_panel._route_id == route.id


def test_apply_route_length_through_window_changes_length_and_undoes(qapp):
    win = MainWindow()
    a, b = _two_straights(win.document)
    win.scene.add_instance_item(a.id)
    win.scene.add_instance_item(b.id)
    route = win.document.add_route(a.id, "o2", b.id, "o1", "strip")
    win.scene.add_route_item(route.id)
    natural = win.route_length_um(win.document.routes[route.id])

    win._on_route_length_applied(route.id, 200.0, "µm", True)
    updated = win.document.routes[route.id]
    assert updated.goal_length_um == 200.0
    assert win.route_length_um(updated) > natural + 50

    win.undo_stack.undo()
    assert win.document.routes[route.id].goal_length_um is None
    assert abs(win.route_length_um(win.document.routes[route.id]) - natural) < 1.0
