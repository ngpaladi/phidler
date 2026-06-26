"""mkdocs build hooks.

Regenerates the embedded screenshots (docs/screenshots/) before each site
build, so the documentation images always match the current UI.

Screenshot capture needs PySide6/Qt and the optional FDTD extras, and Qt needs
its bundled library path on LD_LIBRARY_PATH (the same gotcha run.sh handles).
When any of that is missing — e.g. a CI docs build without the extras — this
hook logs a warning and leaves the committed screenshots in place rather than
failing the whole build. It can also be skipped explicitly (handy for fast
`mkdocs serve` edit loops, since the FDTD captures run real simulations):

    PHIDLER_SKIP_SCREENSHOTS=1 mkdocs serve
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

log = logging.getLogger("mkdocs.hooks.screenshots")


def on_pre_build(config, **kwargs) -> None:
    if os.environ.get("PHIDLER_SKIP_SCREENSHOTS"):
        log.info("PHIDLER_SKIP_SCREENSHOTS set — using the committed screenshots")
        return

    # Qt has no display in a docs build; render into an off-screen buffer.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    script = Path(__file__).parent / "capture_screenshots.py"
    try:
        spec = importlib.util.spec_from_file_location("phidler_capture_screenshots", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # import side effects: activate PDK, build QApplication
        module.regenerate_all()
        log.info("Regenerated documentation screenshots")
    except Exception as exc:  # noqa: BLE001 — never fail the docs build over screenshots
        log.warning(
            "Skipped screenshot regeneration (%s: %s) — using the committed images. "
            "Run via ./run.sh so Qt's library path is set, with the FDTD extras installed.",
            type(exc).__name__,
            exc,
        )
