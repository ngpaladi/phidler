from phidler.main_window import MainWindow
from phidler.panels.console_panel import ConsolePanel


def _submit(console: ConsolePanel, line: str) -> None:
    console.input.setText(line)
    console._on_return()


def test_simple_expression_prints_result(qapp):
    console = ConsolePanel({})
    _submit(console, "1 + 1")
    assert "2" in console.output.toPlainText().splitlines()[-1]


def test_namespace_values_are_accessible(qapp):
    console = ConsolePanel({"x": 41})
    _submit(console, "print(x + 1)")
    assert "42" in console.output.toPlainText()


def test_multiline_block_waits_for_blank_line_before_executing(qapp):
    console = ConsolePanel({})
    _submit(console, "for i in range(3):")
    assert console._buffer  # still accumulating, not yet executed
    _submit(console, "    print(i)")
    assert console._buffer  # still incomplete — needs the blank line
    _submit(console, "")
    assert not console._buffer  # now executed and cleared
    text = console.output.toPlainText()
    assert "0" in text and "1" in text and "2" in text


def test_exception_is_reported_not_raised(qapp):
    console = ConsolePanel({})
    _submit(console, "1 / 0")  # must not raise out of _on_return
    assert "ZeroDivisionError" in console.output.toPlainText()


def test_syntax_error_is_reported_not_raised(qapp):
    console = ConsolePanel({})
    _submit(console, "def f(:")
    assert "SyntaxError" in console.output.toPlainText()


def test_quit_does_not_kill_the_process(qapp):
    """quit()/exit() raise SystemExit, which (confirmed empirically while
    building this) propagates straight through code.InteractiveInterpreter
    .runsource() uncaught — it would silently kill the whole desktop app,
    not just the console, if not explicitly caught here."""
    console = ConsolePanel({})
    _submit(console, "quit()")  # must not raise/exit the test process
    assert "SystemExit" in console.output.toPlainText()
    _submit(console, "1 + 1")  # interpreter must still be usable afterward
    assert "2" in console.output.toPlainText().splitlines()[-1]


def test_history_recall_with_up_and_down_arrows(qapp):
    console = ConsolePanel({})
    _submit(console, "first_command")
    _submit(console, "second_command")

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    def press(key):
        event = QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier)
        console.input.keyPressEvent(event)

    press(Qt.Key_Up)
    assert console.input.text() == "second_command"
    press(Qt.Key_Up)
    assert console.input.text() == "first_command"
    press(Qt.Key_Down)
    assert console.input.text() == "second_command"
    press(Qt.Key_Down)
    assert console.input.text() == ""


def test_place_helper_through_main_window_console_renders_instance(qapp):
    win = MainWindow()
    console = win.console_panel
    _submit(console, "inst = place('straight', length=12.0, x=5.0, y=3.0)")

    assert len(win.document.instances) == 1
    inst_id = next(iter(win.document.instances))
    assert inst_id in win.scene.items_by_inst  # actually rendered, not just modeled
    t = win.document.get_transform(inst_id)
    import math

    assert math.isclose(t.x, 5.0) and math.isclose(t.y, 3.0)


def test_route_helper_through_main_window_console_renders_route(qapp):
    win = MainWindow()
    console = win.console_panel
    _submit(console, "a = place('straight', length=10.0)")
    _submit(console, "b = place('straight', length=10.0, x=0.0, y=20.0, rotation=90.0)")
    _submit(console, "route(a.id, 'o2', b.id, 'o1')")

    assert len(win.document.routes) == 1
    route_id = next(iter(win.document.routes))
    assert route_id in win.scene.route_items


def test_console_namespace_includes_gf_doc_scene_view_win(qapp):
    win = MainWindow()
    console = win.console_panel
    _submit(console, "print(doc is win.document, scene is win.scene, view is win.view)")
    assert "True True True" in console.output.toPlainText()
    _submit(console, "print(gf.__name__)")
    assert "gdsfactory" in console.output.toPlainText()


def test_console_toggle_action_shows_in_view_menu(qapp):
    win = MainWindow()
    assert win.console_toggle_action.text() == "Console"
