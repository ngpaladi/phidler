"""Persisted list of recently opened/saved projects, for the startup window.

Backed by QSettings (per-user, survives restarts). The functions take an
optional QSettings so tests can pass a throwaway one instead of touching the
real user settings.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings

_ORG, _APP = "phidler", "phidler"
_KEY = "recent_projects"
MAX_RECENT = 8


def _settings(settings: QSettings | None) -> QSettings:
    return settings if settings is not None else QSettings(_ORG, _APP)


def load_recent(settings: QSettings | None = None, *, existing_only: bool = True) -> list[str]:
    """Recent project paths, most-recent first. By default drops any whose file
    no longer exists (moved/deleted), so the startup list stays clickable."""
    stored = _settings(settings).value(_KEY, [])
    if isinstance(stored, str):  # QSettings collapses a 1-element list to a bare string
        stored = [stored]
    paths = [str(p) for p in (stored or [])]
    if existing_only:
        paths = [p for p in paths if os.path.exists(p)]
    return paths


def add_recent(path: str, settings: QSettings | None = None) -> list[str]:
    """Record `path` as the most-recent project: moved to the front, de-duped,
    list capped at MAX_RECENT. Returns the new list."""
    store = _settings(settings)
    path = os.path.abspath(os.path.expanduser(path))
    paths = [p for p in load_recent(store, existing_only=False) if p != path]
    paths.insert(0, path)
    del paths[MAX_RECENT:]
    store.setValue(_KEY, paths)
    return paths
