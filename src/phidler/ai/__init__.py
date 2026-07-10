"""Optional AI assistant for phidler.

Two cooperating pieces, both entirely optional — without them the app runs
exactly as before:

* ``mcp_server.PhidlerMcpServer`` — an in-process MCP server (HTTP) that exposes
  phidler's *live* scripting session to an MCP client. Its main tool,
  ``run_python``, executes code in the same namespace the Console panel uses, so
  an assistant's edits render on the canvas immediately and flow through the very
  terminal the user types into.
* ``claude_session.ClaudeSession`` — drives the Claude Code CLI (``claude``) in
  headless mode against that server, so the Console panel's "Ask Claude" mode can
  turn a natural-language request into live layout edits.

Availability is gated: the MCP server needs the ``mcp`` extra, and the assistant
needs the ``claude`` binary on PATH. When either is missing the Console panel
keeps working as a plain Python console and the AI mode is disabled with a
tooltip that says what to install.
"""

from __future__ import annotations

import shutil


def mcp_available() -> bool:
    """Whether the in-process MCP server can be built here — needs the ``mcp``
    SDK and the ASGI server it serves over (both from the ``ai`` extra)."""
    try:
        import mcp  # noqa: F401
        import uvicorn  # noqa: F401

        return True
    except Exception:
        return False


def claude_cli_available() -> bool:
    """Whether the Claude Code CLI (``claude``) is on PATH."""
    return shutil.which("claude") is not None


def ai_available() -> bool:
    """Whether the full assistant can run: both the MCP server deps and the
    ``claude`` binary. The Console panel only offers "Ask Claude" when True."""
    return mcp_available() and claude_cli_available()


def unavailable_reason() -> str | None:
    """A one-line, user-facing explanation of what's missing, or None when the
    assistant is fully available. Used as the disabled-mode tooltip."""
    missing = []
    if not mcp_available():
        missing.append('the MCP server deps (pip install -e ".[ai]")')
    if not claude_cli_available():
        missing.append("the Claude Code CLI (`claude` on PATH — see claude.com/code)")
    if not missing:
        return None
    return "AI assistant disabled — install " + " and ".join(missing) + "."
