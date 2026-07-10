"""In-process MCP server exposing phidler's live scripting session.

The server runs in a background thread (its own asyncio loop + uvicorn), but
every document/scene mutation must happen on the Qt GUI thread. ``GuiInvoker``
bridges the two: a tool handler hands a callable to the invoker, a queued Qt
signal runs it on the GUI thread, and a Future carries the result back. So an
assistant calling the ``run_python`` tool ends up executing code in the same
interpreter — and echoing into the same Console panel — that the user types
into, with the results rendered live on the canvas.

Everything here is imported lazily/optionally: ``phidler.ai.mcp_available()``
gates construction, so a build without the ``mcp`` extra never imports this at
a point that would fail.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from concurrent.futures import Future
from typing import Callable

from PySide6.QtCore import QObject, Signal

# The stable server name. Claude Code namespaces MCP tools as
# ``mcp__<server>__<tool>``; allowed_tool_names() below builds those.
SERVER_NAME = "phidler"
_TOOL_NAMES = ("run_python", "describe_session", "list_components")


class GuiInvoker(QObject):
    """Runs callables on the GUI (Qt main) thread on behalf of other threads.

    Construct it on the GUI thread. ``call(fn)`` may be invoked from any thread;
    it blocks until ``fn`` has run on the GUI thread and returns its result (or
    re-raises its exception)."""

    _submit = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Emitted from the server thread, this object lives in the GUI thread, so
        # Qt delivers it as a queued connection and _run executes on the GUI
        # thread — exactly where document mutations are safe.
        self._submit.connect(self._run)

    def _run(self, item) -> None:
        fn, future = item
        try:
            future.set_result(fn())
        except Exception as exc:  # noqa: BLE001 - forwarded to the calling thread
            future.set_exception(exc)

    def call(self, fn: Callable, timeout: float = 120.0):
        future: "Future" = Future()
        self._submit.emit((fn, future))
        return future.result(timeout)


def _free_tcp_port(host: str = "127.0.0.1") -> int:
    """An OS-assigned free port on ``host`` — bound-then-released, so there's a
    tiny race, but the server binds it again immediately on start()."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


class PhidlerMcpServer:
    """Serves phidler's live session over MCP (streamable HTTP) on localhost.

    Callbacks (all invoked on the GUI thread via ``invoker``):
      * ``run_python(code) -> str`` — execute code in the Console namespace and
        return its captured output.
      * ``describe_session() -> str`` — a summary of the current layout.
      * ``list_components(filter) -> list[str]`` — placeable component names.
    """

    def __init__(
        self,
        invoker: GuiInvoker,
        *,
        run_python: Callable[[str], str],
        describe_session: Callable[[], str],
        list_components: Callable[[str], list],
        host: str = "127.0.0.1",
    ) -> None:
        self._invoker = invoker
        self._run_python = run_python
        self._describe_session = describe_session
        self._list_components = list_components
        self.host = host
        self.port = _free_tcp_port(host)
        self._thread: threading.Thread | None = None
        self._server = None  # uvicorn.Server

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def mcp_config_json(self) -> str:
        """The ``--mcp-config`` payload pointing an MCP client at this server."""
        return json.dumps({"mcpServers": {SERVER_NAME: {"type": "http", "url": self.url}}})

    @staticmethod
    def allowed_tool_names() -> list[str]:
        """Fully-qualified tool names to pass to ``claude --allowedTools`` so the
        assistant can call them without an (unanswerable, in headless mode)
        permission prompt."""
        return [f"mcp__{SERVER_NAME}__{name}" for name in _TOOL_NAMES]

    # -- server plumbing -------------------------------------------------------

    def _build_app(self):
        from mcp.server.fastmcp import FastMCP

        inv = self._invoker
        mcp = FastMCP(SERVER_NAME, host=self.host, port=self.port)

        @mcp.tool()
        async def run_python(code: str) -> str:
            """Execute Python in phidler's live scripting console and return its
            printed output. This is the primary way to change the layout.

            The code runs in the same namespace as the Console panel, with these
            names available: doc (LayoutDocument), scene, view, win (MainWindow),
            gf (gdsfactory), place(spec, x=0, y=0, rotation=0, mirror=False,
            **kwargs) -> instance, and route(inst_a_id, port_a, inst_b_id,
            port_b, cross_section='strip'). Changes render on the canvas
            immediately. Use print(...) to inspect state — the printed text is
            returned to you. Call describe_session first if unsure of the layout.
            """
            # to_thread keeps the server's event loop free while the (blocking)
            # GUI-thread hop runs; on a single-user local box the serialization
            # this implies is fine.
            return await asyncio.to_thread(inv.call, lambda: self._run_python(code))

        @mcp.tool()
        async def describe_session() -> str:
            """Summarise the live phidler project: platform/settings, every
            placed instance (id, component, position, ports) and route, plus the
            scripting names available to run_python. Read this before editing."""
            return await asyncio.to_thread(inv.call, self._describe_session)

        @mcp.tool()
        async def list_components(filter: str = "") -> list[str]:
            """List placeable gdsfactory component names (optionally filtered by a
            case-insensitive substring) that can be passed to place()."""
            return await asyncio.to_thread(inv.call, lambda: self._list_components(filter))

        return mcp.streamable_http_app()

    def start(self) -> None:
        import uvicorn

        app = self._build_app()
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)

        def _serve() -> None:
            # A dedicated event loop for this thread (uvicorn.run() would make one
            # too, but set it explicitly so nothing inherits the GUI's loop).
            asyncio.set_event_loop(asyncio.new_event_loop())
            self._server.run()

        self._thread = threading.Thread(target=_serve, name="phidler-mcp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Cooperative shutdown; the thread is a daemon so it also dies with the
        # process if this never gets called.
        if self._server is not None:
            self._server.should_exit = True
