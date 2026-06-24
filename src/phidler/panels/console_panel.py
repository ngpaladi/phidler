from __future__ import annotations

import code
import io
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

_WELCOME = (
    "Phidler scripting console — Python, evaluated against the live session.\n"
    "Available: gf (gdsfactory), doc (LayoutDocument), scene (LayoutScene),\n"
    "view (LayoutView), win (MainWindow), place(spec, x=, y=, rotation=,\n"
    "mirror=, **kwargs), route(inst_a, port_a, inst_b, port_b, cross_section=).\n"
    "Everything here — doc/scene directly, or place()/route() — is real and\n"
    "immediate but bypasses the undo stack. Only the palette/toolbar/menu\n"
    "actions push undoable commands.\n"
)


class _HistoryLineEdit(QLineEdit):
    """Plain QLineEdit plus Up/Down history recall, like a shell."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._history: list[str] = []
        self._index = 0

    def commit_to_history(self, text: str) -> None:
        if text:
            self._history.append(text)
        self._index = len(self._history)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Up:
            if self._index > 0:
                self._index -= 1
                self.setText(self._history[self._index])
            return
        if event.key() == Qt.Key_Down:
            if self._index < len(self._history) - 1:
                self._index += 1
                self.setText(self._history[self._index])
            else:
                self._index = len(self._history)
                self.clear()
            return
        super().keyPressEvent(event)


class ConsolePanel(QWidget):
    """An interactive Python console running against a caller-supplied
    namespace (gf/doc/scene/view/win in practice — see main_window.py).

    Uses code.InteractiveInterpreter for multi-line/continuation handling
    (a `for`/`if`/`def` block correctly waits for a blank line before
    executing) rather than reimplementing that — verified empirically that
    each call needs the *accumulated* buffer, not just the latest line, and
    that quit()/exit() raise SystemExit which must be caught here or it
    would silently kill the whole desktop app, not just the console.
    """

    def __init__(self, namespace: dict, parent=None) -> None:
        super().__init__(parent)
        self._interpreter = code.InteractiveInterpreter(namespace)
        self._buffer: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Monospace"))
        self.output.setPlainText(_WELCOME)
        layout.addWidget(self.output)

        self.input = _HistoryLineEdit()
        self.input.setFont(QFont("Monospace"))
        self.input.setPlaceholderText(">>> ")
        self.input.returnPressed.connect(self._on_return)
        layout.addWidget(self.input)

    def _append(self, text: str) -> None:
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(QTextCursor.End)

    def _on_return(self) -> None:
        line = self.input.text()
        self.input.clear()
        self.input.commit_to_history(line)

        prompt = "... " if self._buffer else ">>> "
        self._append(f"\n{prompt}{line}\n")

        self._buffer.append(line)
        source = "\n".join(self._buffer)

        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = captured = io.StringIO()
        try:
            needs_more = self._interpreter.runsource(source, "<console>")
        except SystemExit:
            needs_more = False
            captured.write("SystemExit caught — use the window controls to close Phidler instead.\n")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        output_text = captured.getvalue()
        if output_text:
            self._append(output_text)

        if not needs_more:
            self._buffer.clear()
