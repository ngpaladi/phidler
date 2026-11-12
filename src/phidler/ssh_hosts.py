"""List the hosts a user has named in their OpenSSH client config.

Remote runs address a machine by its ``Host`` alias (see fdtd_remote), so the
remote-server controls offer the aliases found in ``~/.ssh/config`` instead of
making the user retype them. Only the names are read: phidler still shells out
to ``ssh <alias>`` and leaves every other setting (HostName, User, keys,
ProxyJump …) to ssh itself.

Patterns (``*``, ``?``, ``!negated``) are skipped, since they aren't hosts one
can connect to, and ``Include`` directives are followed the way ssh resolves
them: relative paths are taken from ``~/.ssh``, and globs are expanded.
"""

from __future__ import annotations

import glob
import os
import shlex
from pathlib import Path

DEFAULT_SSH_CONFIG = "~/.ssh/config"
_MAX_INCLUDE_DEPTH = 8  # guards against an Include cycle


def _split_directive(line: str) -> tuple[str, list[str]]:
    """Split one config line into (lower-cased keyword, arguments). ssh accepts
    ``Keyword value``, ``Keyword=value`` and quoted values; comments and blank
    lines give an empty keyword."""
    line = line.strip()
    if not line or line.startswith("#"):
        return "", []
    try:
        parts = shlex.split(line, comments=False)
    except ValueError:  # unbalanced quote: fall back to whitespace splitting
        parts = line.split()
    if not parts:
        return "", []
    head = parts[0]
    if "=" in head:
        keyword, _, first = head.partition("=")
        args = ([first] if first else []) + parts[1:]
    else:
        keyword, args = head, parts[1:]
        if args and args[0] == "=":
            args = args[1:]
        elif args and args[0].startswith("="):
            args[0] = args[0][1:]
    return keyword.lower(), args


def _is_concrete(pattern: str) -> bool:
    return bool(pattern) and not pattern.startswith("!") and not any(c in pattern for c in "*?")


def _collect(path: Path, ssh_dir: Path, seen: list[str], depth: int) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        keyword, args = _split_directive(line)
        if keyword == "host":
            for name in args:
                if _is_concrete(name) and name not in seen:
                    seen.append(name)
        elif keyword == "include" and depth < _MAX_INCLUDE_DEPTH:
            for arg in args:
                target = Path(os.path.expanduser(arg))
                if not target.is_absolute():
                    target = ssh_dir / target
                for match in sorted(glob.glob(str(target))):
                    _collect(Path(match), ssh_dir, seen, depth + 1)


def list_ssh_hosts(config_path: str | os.PathLike | None = None) -> list[str]:
    """The concrete ``Host`` aliases in the user's ssh config, in file order and
    without duplicates. A missing or unreadable config gives an empty list."""
    path = Path(os.path.expanduser(str(config_path or DEFAULT_SSH_CONFIG)))
    seen: list[str] = []
    _collect(path, path.parent, seen, 0)
    return seen
