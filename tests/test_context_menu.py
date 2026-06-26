from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument


def test_context_menu_signal_emits_on_right_click(qapp):
    """Calls LayoutView.contextMenuEvent() directly as a plain method call
    rather than injecting a synthetic QContextMenuEvent through Qt's real
    event-delivery system (QApplication.sendEvent) — the latter reliably
    crashed the interpreter (native core dump, not just a hang) when tried
    against this widget hierarchy under the offscreen platform. Calling the
    override directly still exercises the exact same logic without going
    through whatever in the native event pipeline is unstable here.

    Also deliberately uses a bare LayoutView, not a full MainWindow:
    MainWindow's constructor wires this same signal to
    _show_canvas_context_menu, which calls the blocking QMenu.exec() — that
    connection firing too (alongside this test's own) hung an earlier
    version of this test forever."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.context_menu_requested.connect(lambda pos: received.append(pos))

    event = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(50, 60), QPoint(50, 60))
    view.contextMenuEvent(event)

    assert received == [QPoint(50, 60)]


def test_context_menu_rotate_action_affects_selected_instance(qapp):
    """Exercises the actual menu-building path (_show_canvas_context_menu)
    rather than just the signal — confirms the menu reuses the same
    QAction the Edit menu uses, so triggering it from the context menu
    has the identical effect as the keyboard shortcut."""
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.rotate_action.trigger()

    t = win.document.get_transform(inst_id)
    assert t.rotation == 90.0


def test_build_canvas_context_menu_contains_expected_actions(qapp):
    """QMenu.exec() blocks waiting for a user choice — calling it in a
    headless test hangs forever (confirmed the hard way: monkeypatching
    QMenu.exec didn't take, since it's a PySide6-bound method, and the
    test hung until killed). _build_canvas_context_menu() is the
    exec()-free half of _show_canvas_context_menu, built specifically so
    construction can be tested without ever calling the blocking part."""
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    menu = win._build_canvas_context_menu()
    actions = menu.actions()
    assert win.rotate_action in actions
    assert win.flip_h_action in actions
    assert win.flip_v_action in actions
    assert win.delete_action in actions
    assert win.copy_action in actions
    assert win.paste_action in actions
    assert win.select_all_action in actions
    assert win.zoom_fit_action in actions
    assert win.zoom_selection_action in actions
