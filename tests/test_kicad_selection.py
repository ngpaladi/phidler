"""KiCad-style rubber-band selection: dragging right = window (fully enclose an
item to select it); dragging left = crossing (touch it to select it)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent, QPainterPath

from phidler.main_window import MainWindow


def _press(view, x: int, y: int) -> None:
    p = QPointF(x, y)
    ev = QMouseEvent(
        QEvent.MouseButtonPress, p, view.viewport().mapToGlobal(QPoint(x, y)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
    )
    view.mousePressEvent(ev)


def _release(view, x: int, y: int) -> None:
    p = QPointF(x, y)
    ev = QMouseEvent(
        QEvent.MouseButtonRelease, p, view.viewport().mapToGlobal(QPoint(x, y)),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
    )
    view.mouseReleaseEvent(ev)


def _two_straights(win):
    a = win.document.add_instance("straight", {"length": 10.0}, x=0.0, y=0.0)
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("straight", {"length": 10.0}, x=8.0, y=0.0)
    win.scene.add_instance_item(b.id)
    return a.id, b.id


# -- direction -> selection mode ----------------------------------------------


def test_drag_right_is_window_drag_left_is_crossing(qapp):
    win = MainWindow()
    win.view.resize(600, 600)
    win.view.show()
    hints = []
    win.view.select_mode_changed.connect(hints.append)

    win.view._rubber_origin = QPoint(100, 100)
    win.view._rubber_mode = None

    win.view._update_rubber_band_mode(QPoint(200, 100))  # dragging right
    assert win.view.rubberBandSelectionMode() == Qt.ContainsItemBoundingRect
    assert "Window" in hints[-1]

    win.view._update_rubber_band_mode(QPoint(40, 100))  # back left of the origin
    assert win.view.rubberBandSelectionMode() == Qt.IntersectsItemBoundingRect
    assert "Crossing" in hints[-1]


def test_no_horizontal_move_defaults_to_window(qapp):
    win = MainWindow()
    win.view._rubber_origin = QPoint(100, 100)
    win.view._rubber_mode = None
    win.view._update_rubber_band_mode(QPoint(100, 300))  # straight down, no x change
    assert win.view.rubberBandSelectionMode() == Qt.ContainsItemBoundingRect


# -- the two modes actually select differently ---------------------------------


def test_window_encloses_crossing_touches(qapp):
    win = MainWindow()
    win.view.resize(600, 600)
    win.view.show()
    a_id, b_id = _two_straights(win)
    ra = win.scene.items_by_inst[a_id].sceneBoundingRect()

    # A band that fully covers A but only clips B's left end.
    band = QRectF(ra.left() - 1, ra.top() - 1, ra.width() + 2, ra.height() + 2)
    path = QPainterPath()
    path.addRect(band)

    def select(mode):
        win.scene.clearSelection()
        win.scene.setSelectionArea(path, Qt.ReplaceSelection, mode, win.view.transform())
        return sorted(i.inst_id for i in win.scene.selectedItems() if getattr(i, "inst_id", None) in win.document.instances)

    assert select(Qt.ContainsItemBoundingRect) == [a_id]  # window: only the enclosed one
    assert select(Qt.IntersectsItemBoundingRect) == [a_id, b_id]  # crossing: both touched


# -- rubber-band lifecycle -----------------------------------------------------


def test_rubber_origin_set_on_empty_press_and_cleared_on_release(qapp):
    win = MainWindow()
    win.view.resize(600, 600)
    win.view.show()

    # Press on empty canvas starts a rubber band.
    _press(win.view, 300, 300)
    assert win.view._rubber_origin is not None
    _release(win.view, 320, 300)
    assert win.view._rubber_origin is None


def test_press_on_an_item_starts_no_rubber_band(qapp):
    win = MainWindow()
    win.view.resize(600, 600)
    win.view.show()
    a_id, _ = _two_straights(win)
    item = win.scene.items_by_inst[a_id]
    center = item.mapToScene(item.boundingRect().center())
    win.view.centerOn(center)
    vp = win.view.mapFromScene(center)

    _press(win.view, vp.x(), vp.y())
    assert win.view._rubber_origin is None  # clicked an item -> move, not a band
    _release(win.view, vp.x(), vp.y())
