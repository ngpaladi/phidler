import math

from PySide6.QtCore import QPoint

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument


def test_report_cursor_position_matches_map_to_scene(qapp):
    """The real, headless-testable core: the transform itself. Called
    directly with a known viewport point rather than via a synthetic
    QMouseEvent — injecting native events under the offscreen platform has
    already proven unstable elsewhere in this codebase (a QContextMenuEvent
    sent via QApplication.sendEvent segfaulted), so this verifies the
    coordinate math without going through Qt's event-delivery system."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()
    view.scale(3, 3)  # also check it holds at a non-default zoom level

    received = []
    view.cursor_position_changed.connect(lambda x, y: received.append((x, y)))

    point = QPoint(123, 77)
    view.report_cursor_position(point)

    expected = view.mapToScene(point)
    assert len(received) == 1
    assert math.isclose(received[0][0], expected.x(), abs_tol=1e-9)
    assert math.isclose(received[0][1], expected.y(), abs_tol=1e-9)


def test_main_window_updates_cursor_label_from_signal(qapp):
    win = MainWindow()
    assert win.cursor_pos_label.text() == ""

    win._on_cursor_position_changed(12.5, -3.25)

    assert "12.500" in win.cursor_pos_label.text()
    assert "-3.250" in win.cursor_pos_label.text()


def test_mouse_move_event_reports_position_without_panning(qapp):
    """mouseMoveEvent must call report_cursor_position on every move, not
    just while panning — exercised via a direct method call (see module
    docstring above on why synthetic QMouseEvents are avoided) by invoking
    the override with a constructed event object built without going
    through sendEvent."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.cursor_position_changed.connect(lambda x, y: received.append((x, y)))

    event = QMouseEvent(
        QMouseEvent.MouseMove,
        QPointF(40, 40),
        QPointF(40, 40),
        Qt.NoButton,
        Qt.NoButton,
        Qt.NoModifier,
    )
    view.mouseMoveEvent(event)

    assert len(received) == 1
