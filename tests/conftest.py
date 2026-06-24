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
