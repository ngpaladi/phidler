import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from phidler.app import activate_pdk

activate_pdk()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def _private_coupler_cache(tmp_path, monkeypatch):
    """Keep the LN coupler-length cache (phidler.chip_circuit) out of the
    user's ~/.cache during tests."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    try:
        from phidler import chip_circuit
    except Exception:  # pragma: no cover
        return
    monkeypatch.setattr(chip_circuit, "_coupler_lengths", {})
