from __future__ import annotations

import code
import io
import sys
from contextlib import nullcontext

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

_WELCOME = (
    "Phidler scripting console — Python, evaluated against the live session.\n"
    "Available: gf (gdsfactory), doc (LayoutDocument), scene (LayoutScene),\n"
    "view (LayoutView), win (MainWindow), place(spec, x=, y=, rotation=,\n"
    "mirror=, **kwargs), route(inst_a, port_a, inst_b, port_b, cross_section=).\n"
    "place() and route() are undoable — Ctrl+Z reverts a whole console entry (or\n"
    "a whole Claude action) as one step. Editing doc/scene directly still applies\n"
    "immediately but bypasses undo.\n"
)

_AI_WELCOME = (
    "\nAsk Claude: switch the dropdown to “Ask Claude” and type a request in\n"
    "plain English (e.g. “add a 2×2 MMI at the origin and route it to the\n"
    "input grating”). Claude drives the layout through this same console — you\n"
    "see the Python it runs, prefixed “claude ▸”, and the result on the canvas.\n"
)

# Prefix marking code the assistant ran, so it's visually distinct from the
# user's own >>> lines while still clearly flowing through this one terminal.
_AI_CODE_PROMPT = "claude ▸ "


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

    When a ClaudeSession is attached (attach_claude), a mode dropdown adds an
    "Ask Claude" mode: a natural-language prompt is sent to the Claude Code CLI,
    whose edits run back through *this* interpreter via run_python_from_agent —
    so the AI's actions appear in this terminal exactly like typed commands. The
    assistant integration is optional; with no session attached the panel is a
    plain Python console, unchanged.
    """

    def __init__(self, namespace: dict, command_group=None, parent=None) -> None:
        super().__init__(parent)
        self._interpreter = code.InteractiveInterpreter(namespace)
        self._buffer: list[str] = []
        # Optional factory ``label -> context manager`` (main_window supplies the
        # undo grouper's .group): wrapping an execution in it makes every
        # place()/route() the code runs land in one undo macro, so a console entry
        # or a Claude action is a single Ctrl+Z. None => no grouping (plain REPL).
        self._command_group = command_group
        self._claude = None  # the live ClaudeSession, once wired
        # Zero-arg callable returning a ClaudeSession (building/starting the MCP
        # server on first use) or None on failure. Set by set_claude_provider so
        # the server is stood up lazily — only when the user actually switches to
        # "Ask Claude" — rather than on every app/window start-up.
        self._claude_provider = None
        # Whether "Ask Claude" mode is offered at all. An explicit flag rather
        # than reading mode_combo.isVisible(), which is False for a widget whose
        # window isn't shown (e.g. under test).
        self._ai_enabled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Monospace"))
        self.output.setPlainText(_WELCOME)
        layout.addWidget(self.output)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)

        # Mode selector — hidden until a ClaudeSession is attached, so a build
        # without the AI assistant looks and behaves exactly as before.
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Python", "Ask Claude"])
        self.mode_combo.setVisible(False)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        input_row.addWidget(self.mode_combo)

        self.input = _HistoryLineEdit()
        self.input.setFont(QFont("Monospace"))
        self.input.setPlaceholderText(">>> ")
        self.input.setToolTip(
            "Full Python against the live session. Use gf (gdsfactory), doc, "
            "scene, view, win, plus place(spec, x=, y=, rotation=, mirror=, "
            "**kwargs) and route(inst_a, port_a, inst_b, port_b, "
            "cross_section=). Up/Down recall history; multi-line blocks need a "
            "blank line to run. place()/route() are undoable (Ctrl+Z reverts the "
            "whole entry); direct doc/scene edits bypass undo."
        )
        self.input.returnPressed.connect(self._on_return)
        input_row.addWidget(self.input, stretch=1)

        layout.addLayout(input_row)

    # -- assistant wiring ------------------------------------------------------

    def set_claude_provider(self, provider) -> None:
        """Enable "Ask Claude" mode, building the backing ClaudeSession lazily.

        ``provider`` is a zero-arg callable returning a ClaudeSession (starting
        the in-process MCP server on first call) or None if it can't. The mode
        selector appears now; the server isn't started until the user actually
        switches to "Ask Claude". Optional — called by main_window only when the
        AI assistant is available.
        """
        self._claude_provider = provider
        self._ai_enabled = True
        self.mode_combo.setVisible(True)
        self.output.appendPlainText(_AI_WELCOME)

    def attach_claude(self, session) -> None:
        """Wire a ClaudeSession directly (eager path). set_claude_provider is the
        usual entry point; this is used when a session already exists."""
        self._wire_claude(session)
        self._ai_enabled = True
        self.mode_combo.setVisible(True)

    def _wire_claude(self, session) -> None:
        self._claude = session
        session.started.connect(self._on_claude_started)
        session.assistant_text.connect(self._on_claude_text)
        session.tool_activity.connect(self._on_claude_tool)
        session.finished.connect(self._on_claude_finished)
        session.failed.connect(self._on_claude_failed)

    def _ensure_claude(self) -> bool:
        """Make sure a ClaudeSession is wired, building it via the provider on
        first use. Returns False if the assistant can't be started."""
        if self._claude is not None:
            return True
        if self._claude_provider is None:
            return False
        session = self._claude_provider()
        if session is None:
            return False
        self._wire_claude(session)
        return True

    @property
    def _in_ai_mode(self) -> bool:
        return self._ai_enabled and self.mode_combo.currentIndex() == 1

    def _on_mode_changed(self, index: int) -> None:
        if index == 1:
            self.input.setPlaceholderText("Ask Claude to modify the layout…")
            # Stand up the assistant now so its first-use latency is paid on the
            # mode switch, not hidden inside the first message.
            if not self._ensure_claude():
                self._append("\n[AI assistant could not start — staying in Python mode.]\n")
                self.mode_combo.setCurrentIndex(0)
        else:
            self.input.setPlaceholderText(">>> ")

    # -- output helpers --------------------------------------------------------

    def _append(self, text: str) -> None:
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(QTextCursor.End)

    # -- input handling --------------------------------------------------------

    def _on_return(self) -> None:
        line = self.input.text()
        if self._in_ai_mode:
            if not self._ensure_claude():
                self._append("\n[AI assistant unavailable — switch back to Python mode.]\n")
                return
            self._ask_claude(line)
            return
        self._run_python_line(line)

    def _run_python_line(self, line: str) -> None:
        self.input.clear()
        self.input.commit_to_history(line)

        prompt = "... " if self._buffer else ">>> "
        self._append(f"\n{prompt}{line}\n")

        self._buffer.append(line)
        source = "\n".join(self._buffer)

        needs_more, output_text = self._exec(
            source, symbol="single", group_label=self._group_label(source, "Console")
        )
        if output_text:
            self._append(output_text)
        if not needs_more:
            self._buffer.clear()

    @staticmethod
    def _group_label(source: str, prefix: str) -> str:
        """A short undo-macro name from the first non-blank line of ``source``,
        e.g. "Console: place('mmi2x2')" or "Claude: a = place('straight')"."""
        first = next((ln.strip() for ln in source.splitlines() if ln.strip()), "")
        if len(first) > 40:
            first = first[:39] + "…"
        return f"{prefix}: {first}" if first else prefix

    def _exec(self, source: str, *, symbol: str, group_label: str | None = None) -> tuple[bool, str]:
        """Run ``source`` through the interpreter with stdout/stderr captured.

        Returns (needs_more, captured_text). ``symbol`` is "single" for the
        interactive line-at-a-time path (which echoes expression values and can
        ask for more input) and "exec" for running a whole block at once (the
        assistant path — multiple statements, no auto-echo). ``group_label``, with
        a command_group configured, collects the undo commands this run pushes
        into one macro so it undoes as a single step (an incomplete line pushes
        nothing, so no macro is created)."""
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = captured = io.StringIO()
        group = (
            self._command_group(group_label)
            if self._command_group is not None and group_label
            else nullcontext()
        )
        try:
            with group:
                needs_more = self._interpreter.runsource(source, "<console>", symbol)
        except SystemExit:
            needs_more = False
            captured.write("SystemExit caught — use the window controls to close Phidler instead.\n")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        return bool(needs_more), captured.getvalue()

    def run_python_from_agent(self, code_text: str) -> str:
        """Execute assistant-supplied code in this console's interpreter and
        return its captured output. Called on the GUI thread by the MCP server
        (see phidler.ai.mcp_server). Echoes the code and its output into the
        transcript so the user watches the assistant work through this terminal.
        """
        self._append(f"\n{_AI_CODE_PROMPT}{code_text}\n")
        # A whole block at once ("exec"): assistant code may be multi-statement,
        # and it prints what it wants surfaced. A pending multi-line user buffer
        # is left untouched — this is a separate, self-contained execution. The
        # group_label makes everything this tool call places/routes one undo step.
        _needs_more, output_text = self._exec(
            code_text, symbol="exec", group_label=self._group_label(code_text, "Claude")
        )
        if output_text:
            self._append(output_text)
        return output_text or "(no output)"

    # -- assistant chat --------------------------------------------------------

    def _ask_claude(self, prompt: str) -> None:
        prompt = prompt.strip()
        if not prompt or self._claude is None:
            return
        if self._claude.busy:
            self._append("\n[Claude is still working — wait for it to finish.]\n")
            return
        self.input.clear()
        self.input.commit_to_history(prompt)
        self._append(f"\n🧑 {prompt}\n")
        self._claude.send(prompt)

    def _on_claude_started(self) -> None:
        self.input.setEnabled(False)
        self.input.setPlaceholderText("Claude is working…")

    def _on_claude_text(self, text: str) -> None:
        self._append(f"\n🤖 {text.strip()}\n")

    def _on_claude_tool(self, summary: str) -> None:
        self._append(f"   ⚙ {summary}\n")

    def _on_claude_finished(self) -> None:
        self.input.setEnabled(True)
        self.input.setPlaceholderText("Ask Claude to modify the layout…")
        self.input.setFocus()

    def _on_claude_failed(self, message: str) -> None:
        self._append(f"\n[Claude error] {message}\n")
        self._on_claude_finished()
