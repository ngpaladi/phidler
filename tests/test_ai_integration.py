"""Tests for the optional AI assistant (MCP server + Claude console mode) and
the photonfdtd 0.4 backend-flag plumbing.

The MCP end-to-end test is skipped when the ``ai`` extra isn't installed, so the
suite still passes in a minimal environment — mirroring how the app itself
degrades to a plain Python console when the assistant is unavailable.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from phidler import ai
from phidler.fdtd_sim import FdtdParams, SimulationConfig, resolve_accel_flags
from phidler.panels.console_panel import ConsolePanel


# -- availability gating -------------------------------------------------------


def test_availability_helpers_return_bools():
    assert isinstance(ai.mcp_available(), bool)
    assert isinstance(ai.claude_cli_available(), bool)
    assert isinstance(ai.ai_available(), bool)
    reason = ai.unavailable_reason()
    assert reason is None or isinstance(reason, str)
    # ai_available() is exactly "both halves present".
    assert ai.ai_available() == (ai.mcp_available() and ai.claude_cli_available())
    # The reason is present iff the assistant is unavailable.
    assert (reason is None) == ai.ai_available()


# -- console agent execution (feeds through the existing Python terminal) ------


def test_run_python_from_agent_executes_and_echoes(qapp):
    console = ConsolePanel({"state": []})
    out = console.run_python_from_agent("state.append(7)\nprint('sum', sum(state))")
    # Ran against the live namespace...
    assert console._interpreter.locals["state"] == [7]
    # ...returned its printed output to the caller (the MCP tool result)...
    assert "sum 7" in out
    # ...and echoed both the code and the output into the shared transcript,
    # prefixed so the user sees the assistant working through this terminal.
    text = console.output.toPlainText()
    assert "claude ▸ state.append(7)" in text
    assert "sum 7" in text


def test_run_python_from_agent_reports_errors_without_raising(qapp):
    console = ConsolePanel({})
    out = console.run_python_from_agent("1 / 0")  # must not raise
    assert "ZeroDivisionError" in out
    assert "ZeroDivisionError" in console.output.toPlainText()


def test_agent_execution_does_not_disturb_pending_user_buffer(qapp):
    console = ConsolePanel({})
    # User starts a multi-line block but hasn't finished it.
    console.input.setText("for i in range(2):")
    console._on_return()
    assert console._buffer  # accumulating
    # An assistant execution lands in between — it's self-contained and must
    # leave the half-typed user block untouched.
    console.run_python_from_agent("x = 99")
    assert console._buffer  # user's block still pending
    assert console._interpreter.locals["x"] == 99


def test_attach_claude_reveals_mode_selector(qapp):
    console = ConsolePanel({})
    assert console._ai_enabled is False  # no assistant offered without a session

    class _FakeSession:
        busy = False

        def __init__(self):
            from PySide6.QtCore import QObject, Signal

            # Minimal signal-bearing stand-in.
            class S(QObject):
                started = Signal()
                assistant_text = Signal(str)
                tool_activity = Signal(str)
                finished = Signal()
                failed = Signal(str)

            self._s = S()
            for name in ("started", "assistant_text", "tool_activity", "finished", "failed"):
                setattr(self, name, getattr(self._s, name))

    console.attach_claude(_FakeSession())
    assert console.mode_combo.count() == 2
    assert console._ai_enabled is True
    # Mode switching updates the prompt hint and enters AI mode.
    console.mode_combo.setCurrentIndex(1)
    assert console._in_ai_mode is True


def test_claude_provider_is_lazy(qapp):
    """The provider (which would start the MCP server) is only called when the
    user switches to "Ask Claude", not when it's registered."""
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return None  # simulate an assistant that can't start

    console = ConsolePanel({})
    console.set_claude_provider(provider)
    assert console._ai_enabled is True
    assert calls["n"] == 0  # not started just by registering

    console.mode_combo.setCurrentIndex(1)  # user opts in
    assert calls["n"] == 1  # provider invoked lazily
    # Provider returned None, so the console falls back to Python mode.
    assert console.mode_combo.currentIndex() == 0


# -- ClaudeSession stream parsing (without spawning the real CLI) --------------


def test_claude_session_parses_stream_json_events(qapp):
    from phidler.ai.claude_session import ClaudeSession

    session = ClaudeSession('{"mcpServers":{}}', ["mcp__phidler__run_python"])
    texts: list[str] = []
    tools: list[str] = []
    session.assistant_text.connect(texts.append)
    session.tool_activity.connect(tools.append)

    # A text block, then a tool_use block, as claude -p --output-format
    # stream-json emits them (one JSON object per line).
    session._handle_event(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Adding an MMI."}]}})
    )
    session._handle_event(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__phidler__run_python",
                            "input": {"code": "place('mmi2x2')\nprint('ok')"},
                        }
                    ]
                },
            }
        )
    )
    assert texts == ["Adding an MMI."]
    assert len(tools) == 1
    assert "run_python" in tools[0]
    assert "place('mmi2x2')" in tools[0]
    assert "2 lines" in tools[0]


def test_claude_session_ignores_malformed_lines(qapp):
    from phidler.ai.claude_session import ClaudeSession

    session = ClaudeSession('{"mcpServers":{}}', [])
    # A partial/garbage line must not raise.
    session._handle_event("{not json")
    assert session.busy is False


# -- MCP server config surface -------------------------------------------------


@pytest.mark.skipif(not ai.mcp_available(), reason="mcp extra not installed")
def test_mcp_server_config_surface():
    from phidler.ai.mcp_server import PhidlerMcpServer, SERVER_NAME

    tools = PhidlerMcpServer.allowed_tool_names()
    assert tools == [
        f"mcp__{SERVER_NAME}__run_python",
        f"mcp__{SERVER_NAME}__describe_session",
        f"mcp__{SERVER_NAME}__list_components",
    ]


@pytest.mark.skipif(not ai.mcp_available(), reason="mcp extra not installed")
def test_mcp_server_url_and_config_json(qapp):
    import json

    from phidler.ai.mcp_server import SERVER_NAME, GuiInvoker, PhidlerMcpServer

    server = PhidlerMcpServer(
        GuiInvoker(),
        run_python=lambda c: c,
        describe_session=lambda: "",
        list_components=lambda f: [],
    )
    assert server.url.startswith("http://127.0.0.1:")
    assert server.url.endswith("/mcp")
    cfg = json.loads(server.mcp_config_json())
    entry = cfg["mcpServers"][SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"] == server.url


@pytest.mark.skipif(not ai.mcp_available(), reason="mcp extra not installed")
def test_mcp_run_python_tool_executes_through_console(qapp):
    """The core integration: an MCP client calling run_python runs code in the
    live console namespace, on the GUI thread, and gets the output back."""
    from phidler.ai.mcp_server import GuiInvoker, PhidlerMcpServer

    placed: list[str] = []
    namespace = {"place": lambda spec: placed.append(spec) or f"inst<{spec}>", "placed": placed}
    console = ConsolePanel(namespace)

    server = PhidlerMcpServer(
        GuiInvoker(),
        run_python=console.run_python_from_agent,
        describe_session=lambda: f"placed={placed}",
        list_components=lambda f: [n for n in ("mmi1x2", "mmi2x2", "straight") if f in n],
    )
    server.start()

    results: dict = {}

    def client_thread():
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def run():
            await asyncio.sleep(0.5)  # let uvicorn bind
            async with streamablehttp_client(server.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = [t.name for t in (await session.list_tools()).tools]
                    results["tools"] = names
                    r = await session.call_tool(
                        "run_python", {"code": "place('mmi2x2')\nprint('n', len(placed))"}
                    )
                    results["output"] = "".join(
                        c.text for c in r.content if getattr(c, "type", "") == "text"
                    )

        try:
            asyncio.run(run())
        except Exception as exc:  # noqa: BLE001
            results["error"] = repr(exc)
        finally:
            results["done"] = True

    threading.Thread(target=client_thread, daemon=True).start()

    deadline = time.time() + 30
    while not results.get("done") and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    server.stop()

    assert results.get("error") is None, results.get("error")
    assert set(results.get("tools", [])) == {"run_python", "describe_session", "list_components"}
    assert "n 1" in results.get("output", "")
    # The mutation actually happened in the live namespace...
    assert placed == ["mmi2x2"]
    # ...and flowed through the console transcript.
    assert "claude ▸ place('mmi2x2')" in console.output.toPlainText()


# -- photonfdtd 0.4 backend flags ---------------------------------------------


def test_new_backend_params_default_off():
    p = FdtdParams()
    assert p.use_jax is False
    assert p.subpixel is False
    assert p.subpixel_factor == 3
    # SimulationConfig too, so older projects (which never wrote these) load.
    c = SimulationConfig()
    assert c.use_jax is False and c.subpixel is False


def test_resolve_accel_flags_precedence():
    # Plain: both accelerators pass through.
    assert resolve_accel_flags(FdtdParams(use_gpu=True, use_numba=True)) == (True, True)
    # JAX is exclusive of GPU and Numba.
    assert resolve_accel_flags(FdtdParams(use_jax=True, use_gpu=True, use_numba=True)) == (False, False)
    # Subpixel drops Numba (unsupported there) but not GPU.
    assert resolve_accel_flags(FdtdParams(subpixel=True, use_gpu=True, use_numba=True)) == (True, False)
    # Out-of-core is NumPy-only: drops both.
    assert resolve_accel_flags(FdtdParams(out_of_core=True, use_gpu=True, use_numba=True)) == (False, False)


def test_simulation_config_survives_missing_new_fields(qapp):
    """A SimulationConfig built without the 0.4 fields (as an old saved project
    would deserialize) still exposes them via the getattr-guarded defaults the
    FDTD window reads."""
    c = SimulationConfig(wavelength_um=1.55)
    assert getattr(c, "use_jax", False) is False
    assert getattr(c, "subpixel", False) is False
