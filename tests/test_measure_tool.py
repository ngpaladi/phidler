import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.model.document import LayoutDocument


def _click(view, scene_pt: QPointF) -> None:
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(scene_pt))


def test_two_clicks_emit_correct_distance_and_dx_dy(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.measurement_taken.connect(lambda dx, dy, d: received.append((dx, dy, d)))

    view.set_measure_mode(True)
    _click(view, QPointF(0.0, 0.0))
    _click(view, QPointF(3.0, 4.0))

    assert len(received) == 1
    dx, dy, distance = received[0]
    assert math.isclose(dx, 3.0, abs_tol=1e-6)
    assert math.isclose(dy, 4.0, abs_tol=1e-6)
    assert math.isclose(distance, 5.0, abs_tol=1e-6)  # 3-4-5 triangle


def test_click_near_a_port_snaps_to_its_exact_position(qapp):
    """straight's ports are o1 at local (0,0) and o2 at local (10,0).
    Clicking slightly off either port must measure from the port's exact
    center, not the raw click point — confirmed via the snapped distance
    coming out to exactly 10.0, not whatever the imprecise click points
    would naively give."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(a.id)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.measurement_taken.connect(lambda dx, dy, d: received.append((dx, dy, d)))

    view.set_measure_mode(True)
    _click(view, QPointF(0.3, 0.1))  # near o1, not exactly on it
    _click(view, QPointF(9.7, -0.1))  # near o2, not exactly on it

    assert len(received) == 1
    dx, dy, distance = received[0]
    assert math.isclose(dx, 10.0, abs_tol=1e-6)
    assert math.isclose(dy, 0.0, abs_tol=1e-6)
    assert math.isclose(distance, 10.0, abs_tol=1e-6)


def test_measurement_draws_a_line_and_label_on_the_scene(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    view.set_measure_mode(True)
    _click(view, QPointF(0.0, 0.0))
    _click(view, QPointF(5.0, 0.0))

    assert len(view._measure_items) == 2
    for item in view._measure_items:
        assert item.scene() is scene


def test_a_third_click_clears_the_previous_measurement_and_starts_fresh(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.measurement_taken.connect(lambda dx, dy, d: received.append((dx, dy, d)))

    view.set_measure_mode(True)
    _click(view, QPointF(0.0, 0.0))
    _click(view, QPointF(5.0, 0.0))
    assert len(received) == 1
    first_items = list(view._measure_items)

    _click(view, QPointF(1.0, 1.0))  # starts a new pair; clears the old annotation
    for item in first_items:
        assert item.scene() is None  # removed from the scene

    _click(view, QPointF(1.0, 5.0))
    assert len(received) == 2
    dx, dy, distance = received[1]
    assert math.isclose(dx, 0.0, abs_tol=1e-6)
    assert math.isclose(dy, 4.0, abs_tol=1e-6)


def test_escape_cancels_a_pending_first_point_and_exits_measure_mode(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    view.set_measure_mode(True)
    _click(view, QPointF(0.0, 0.0))
    assert view._measure_first_point is not None

    from PySide6.QtGui import QKeyEvent

    escape_event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    view.keyPressEvent(escape_event)

    assert view.measure_mode is False
    assert view._measure_first_point is None


def test_enabling_measure_mode_disables_routing_mode_and_placement(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    view.set_routing_mode(True)
    assert scene.routing_mode is True

    view.set_measure_mode(True)
    assert scene.routing_mode is False

    view.set_measure_mode(False)
    view.arm_placement("straight")
    assert view.armed_component == "straight"

    view.set_measure_mode(True)
    assert view.armed_component is None


def test_enabling_routing_mode_disables_measure_mode(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    view.set_measure_mode(True)
    assert view.measure_mode is True

    view.set_routing_mode(True)
    assert view.measure_mode is False


def test_disabling_measure_mode_clears_any_visible_measurement(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    view.set_measure_mode(True)
    _click(view, QPointF(0.0, 0.0))
    _click(view, QPointF(5.0, 0.0))
    items = list(view._measure_items)
    assert len(items) == 2

    view.set_measure_mode(False)
    assert view._measure_items == []
    for item in items:
        assert item.scene() is None


def test_main_window_measure_action_toggles_view_mode(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    assert win.view.measure_mode is False

    win.measure_action.setChecked(True)
    assert win.view.measure_mode is True
    assert win.measure_action.isChecked() is True

    win.measure_action.setChecked(False)
    assert win.view.measure_mode is False


def test_main_window_shows_distance_in_status_bar(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.view.resize(400, 400)
    win.show()

    win.measure_action.setChecked(True)
    _click(win.view, QPointF(0.0, 0.0))
    _click(win.view, QPointF(3.0, 4.0))

    assert "5.000" in win.statusBar().currentMessage()
