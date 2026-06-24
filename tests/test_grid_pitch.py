import math

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument


def test_snap_rounds_to_nearest_pitch_multiple(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    view.grid_pitch = 0.5
    assert math.isclose(view.snap(1.24), 1.0)
    assert math.isclose(view.snap(1.26), 1.5)
    assert math.isclose(view.snap(-0.26), -0.5)

    view.grid_pitch = 2.0
    assert math.isclose(view.snap(2.9), 2.0)
    assert math.isclose(view.snap(3.1), 4.0)


def test_snap_disabled_returns_value_unchanged(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.grid_pitch = 0.5
    view.snap_enabled = False
    assert view.snap(1.2345) == 1.2345


def test_snap_with_zero_or_negative_pitch_returns_value_unchanged(qapp):
    """drawBackground guards pitch<=0 to avoid an infinite scaling loop;
    snap() must handle it too rather than dividing by zero."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.grid_pitch = 0.0
    assert view.snap(3.7) == 3.7
    view.grid_pitch = -1.0
    assert view.snap(3.7) == 3.7


def test_draw_background_does_not_hang_with_nonpositive_pitch(qapp):
    """Regression guard for the exact hazard flagged during review: a grid
    pitch <= 0 reaching drawBackground's 'while pitch * view_scale < 6:
    pitch *= 10' would loop forever. A UI spinbox is range-limited to a
    positive minimum, but this exercises the defensive check directly."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(200, 200)
    view.show()
    view.grid_pitch = 0.0

    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainter, QPixmap

    pixmap = QPixmap(200, 200)
    painter = QPainter(pixmap)
    view.drawBackground(painter, QRectF(0, 0, 200, 200))  # must return, not hang
    painter.end()


def test_grid_pitch_spinbox_is_range_limited_above_zero(qapp):
    win = MainWindow()
    assert win.grid_pitch_spin.minimum() > 0.0


def test_changing_grid_pitch_spinbox_updates_view(qapp):
    win = MainWindow()
    win.grid_pitch_spin.setValue(2.5)
    assert math.isclose(win.view.grid_pitch, 2.5)


def test_snap_checkbox_toggles_view_snap_enabled(qapp):
    win = MainWindow()
    assert win.view.snap_enabled is True
    win.snap_checkbox.setChecked(False)
    assert win.view.snap_enabled is False
