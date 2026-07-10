"""Drive the Claude Code CLI (``claude``) in headless mode as a chat backend.

Each user message spawns a fresh ``claude -p`` process wired to phidler's
in-process MCP server, streaming newline-delimited JSON that we turn into Qt
signals (assistant text, tool activity, done, error). Successive messages reuse
one session id so the conversation carries over.

Optional: ``ClaudeSession.cli_available()`` gates use; nothing here imports the
``mcp`` SDK (that's the server's job) — this only needs the ``claude`` binary.
"""

from __future__ import annotations

import json
import shutil
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, Signal

# Injected as extra system-prompt guidance so the assistant knows it drives
# phidler through the Python console rather than editing files.
SYSTEM_PROMPT = (
    "You are an assistant embedded in phidler, a photonic integrated circuit "
    "layout tool. You control the user's LIVE layout through MCP tools backed by "
    "phidler's Python scripting console — your edits appear on the canvas "
    "immediately. To change the layout, call mcp__phidler__run_python with Python "
    "that uses: doc (the LayoutDocument), place(spec, x=0, y=0, rotation=0, "
    "mirror=False, **kwargs) -> instance, and route(inst_a_id, port_a, "
    "inst_b_id, port_b, cross_section='strip'). Call mcp__phidler__describe_session "
    "to see the current layout and mcp__phidler__list_components to find component "
    "names. Use only these phidler MCP tools; do not edit files on disk or run "
    "shell commands. Keep prose brief — the user watches the console output your "
    "code produces."
)


class ClaudeSession(QObject):
    """A multi-turn conversation with the Claude Code CLI, one process per turn."""

    started = Signal()
    assistant_text = Signal(str)  # a chunk of assistant prose
    tool_activity = Signal(str)  # a short "using tool X" line
    finished = Signal()  # the current turn completed
    failed = Signal(str)  # the turn errored (message is user-facing)

    def __init__(self, mcp_config_json: str, allowed_tools, *, parent=None) -> None:
        super().__init__(parent)
        self._mcp_config_json = mcp_config_json
        self._allowed_tools = list(allowed_tools)
        # One id for the whole conversation: --session-id creates it on the first
        # turn, --resume continues it thereafter.
        self._session_id = str(uuid4())
        self._started_once = False
        self._proc: QProcess | None = None
        self._out_buf = ""
        self._err_buf = ""
        self._saw_output = False

    @staticmethod
    def cli_available() -> bool:
        return shutil.which("claude") is not None

    @property
    def busy(self) -> bool:
        return self._proc is not None

    def send(self, prompt: str) -> None:
        """Start a turn. Emits started, then assistant_text/tool_activity as they
        stream, then finished (or failed)."""
        if self.busy:
            self.failed.emit("Claude is still working on the previous message.")
            return
        claude = shutil.which("claude")
        if claude is None:
            self.failed.emit("The `claude` CLI is not on PATH.")
            return

        args = [
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",  # required for stream-json in --print mode
            "--mcp-config",
            self._mcp_config_json,
            "--strict-mcp-config",  # ignore user/project MCP config; only ours
            "--allowedTools",
            ",".join(self._allowed_tools),  # pre-approve the phidler tools
            "--append-system-prompt",
            SYSTEM_PROMPT,
        ]
        # Continue the same conversation across successive -p invocations.
        args += (["--resume", self._session_id] if self._started_once else ["--session-id", self._session_id])

        self._out_buf = ""
        self._err_buf = ""
        self._saw_output = False

        proc = QProcess(self)
        proc.setProgram(claude)
        proc.setArguments(args)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        self._started_once = True
        self.started.emit()
        proc.start()

    def cancel(self) -> None:
        if self._proc is not None:
            self._proc.kill()

    # -- stream parsing --------------------------------------------------------

    def _on_stdout(self) -> None:
        if self._proc is None:
            return
        self._out_buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        # stream-json is newline-delimited; keep any trailing partial line.
        *lines, self._out_buf = self._out_buf.split("\n")
        for line in lines:
            line = line.strip()
            if line:
                self._handle_event(line)

    def _on_stderr(self) -> None:
        if self._proc is not None:
            self._err_buf += bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")

    def _handle_event(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        etype = event.get("type")
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        self._saw_output = True
                        self.assistant_text.emit(text)
                elif btype == "tool_use":
                    self._saw_output = True
                    self.tool_activity.emit(_summarize_tool_use(block))
        elif etype == "result":
            # Final text already arrived via the last assistant event; the result
            # event just marks completion (and carries cost/usage we don't show).
            if event.get("is_error"):
                self.assistant_text.emit(str(event.get("result", "")))

    def _on_error(self, _error) -> None:
        # errorOccurred can fire alongside finished; report once, then let
        # _on_finished do the cleanup if it hasn't already.
        if self._proc is None:
            return
        msg = self._proc.errorString() if self._proc else "process error"
        self._fail(f"Could not run claude: {msg}")

    def _on_finished(self, exit_code: int, _status) -> None:
        # Flush any complete trailing line.
        if self._out_buf.strip():
            self._handle_event(self._out_buf.strip())
        self._out_buf = ""
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.deleteLater()
        if exit_code != 0 and not self._saw_output:
            detail = self._err_buf.strip()[-800:] or f"claude exited with code {exit_code}"
            self.failed.emit(detail)
            return
        self.finished.emit()

    def _fail(self, message: str) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.deleteLater()
        self.failed.emit(message)


def _summarize_tool_use(block: dict) -> str:
    """A short one-liner for a tool_use event, e.g. the first line of run_python
    code, so the user sees what the assistant is doing through the console."""
    name = block.get("name", "tool")
    short = name.split("__")[-1]
    args = block.get("input", {}) or {}
    if "code" in args:
        first = str(args["code"]).strip().splitlines()
        head = first[0] if first else ""
        if len(head) > 60:
            head = head[:57] + "…"
        extra = f"  ({len(first)} lines)" if len(first) > 1 else ""
        return f"{short}: {head}{extra}"
    if args:
        return f"{short}({', '.join(f'{k}={v!r}' for k, v in list(args.items())[:2])})"
    return short
