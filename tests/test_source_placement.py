import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QGraphicsItem

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.model.document import LayoutDocument


def _click(view, scene_pt: QPointF) -> None:
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(scene_pt))


def test_click_in_source_mode_emits_placement_request(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.source_placement_requested.connect(lambda x, y: received.append((x, y)))

    view.set_source_mode(True)
    _click(view, QPointF(5.0, -3.0))

    assert len(received) == 1
    x, y = received[0]
    assert math.isclose(x, 5.0, abs_tol=1e-6)
    assert math.isclose(y, -3.0, abs_tol=1e-6)


def test_click_near_a_port_snaps_to_its_exact_position(qapp):
    """straight's ports are o1 at local (0,0) and o2 at local (10,0) —
    same port-snap mechanism measure mode already uses."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(a.id)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.source_placement_requested.connect(lambda x, y: received.append((x, y)))

    view.set_source_mode(True)
    _click(view, QPointF(0.3, 0.3))  # near o1 at (0,0), not exactly on it

    assert len(received) == 1
    x, y = received[0]
    assert math.isclose(x, 0.0, abs_tol=1e-6)
    assert math.isclose(y, 0.0, abs_tol=1e-6)


def test_clicking_does_not_itself_create_a_marker(qapp):
    """The view only reports the click — creating a marker is the
    caller's (FdtdWindow's) job via add_source_marker, e.g. after the
    user fills in the new source's properties."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    view.set_source_mode(True)
    _click(view, QPointF(1.0, 1.0))
    assert len(view._source_markers) == 0


def test_add_remove_clear_source_markers(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    m1 = view.add_source_marker(1.0, 2.0)
    m2 = view.add_source_marker(3.0, 4.0)
    assert len(view._source_markers) == 2
    assert bool(m1.flags() & QGraphicsItem.ItemIgnoresTransformations)

    view.remove_source_marker(m1)
    assert len(view._source_markers) == 1
    assert m2 in view._source_markers

    view.clear_source_markers()
    assert len(view._source_markers) == 0


def test_enabling_source_mode_disables_measure_and_routing_modes(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    view.set_measure_mode(True)
    view.set_source_mode(True)
    assert view.source_mode is True
    assert view.measure_mode is False
    assert scene.routing_mode is False


def test_enabling_measure_mode_disables_source_mode(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    view.set_source_mode(True)
    view.set_measure_mode(True)
    assert view.measure_mode is True
    assert view.source_mode is False


def test_enabling_routing_mode_disables_source_mode(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    view.set_source_mode(True)
    view.set_routing_mode(True)
    assert scene.routing_mode is True
    assert view.source_mode is False


def test_escape_exits_source_mode(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    view.set_source_mode(True)
    event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    view.keyPressEvent(event)
    assert view.source_mode is False


def test_source_mode_changed_signal_emits_on_toggle(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    received = []
    view.source_mode_changed.connect(received.append)
    view.set_source_mode(True)
    view.set_source_mode(False)
    assert received == [True, False]
