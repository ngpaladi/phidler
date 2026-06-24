from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication


def activate_pdk() -> None:
    from gdsfactory.gpdk import get_generic_pdk

    get_generic_pdk().activate()


def main(argv: list[str] | None = None) -> int:
    activate_pdk()

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Phidler")

    from PySide6.QtCore import QTimer

    from phidler.main_window import MainWindow

    window = MainWindow()
    window.show()
    # Deferred via singleShot(0, ...) rather than called directly here:
    # this fires after the event loop starts and the window is already
    # shown, not during MainWindow's own construction — _new_project()
    # opens a *modal* dialog, and triggering that from __init__ would hang
    # every test that constructs a MainWindow (there's no event loop yet
    # for a real user to dismiss it with).
    QTimer.singleShot(0, window._new_project)
    return app.exec()
