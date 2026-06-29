from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication


def activate_pdk() -> None:
    import warnings

    from gdsfactory.gpdk import get_generic_pdk

    # A handful of generic-PDK components (coupler_bend, coupler_ring_bend,
    # ring_double_bend_coupler) deliberately build sub-90° euler bends, which
    # makes gdsfactory's bend_euler emit this nag ("Got 35.0 … use
    # bend_euler_all_angle"). The components are valid and placeable — it's an
    # over-eager upstream warning, not a problem with our use — so filter only
    # this exact message (the angle varies) to keep the console and test output
    # clean without masking any other warning.
    warnings.filterwarnings(
        "ignore",
        message=r"bend_euler angle should be 90 or 180",
        category=UserWarning,
    )

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
    # shown, not during MainWindow's own construction — _show_startup()
    # opens a *modal* dialog, and triggering that from __init__ would hang
    # every test that constructs a MainWindow (there's no event loop yet
    # for a real user to dismiss it with).
    QTimer.singleShot(0, window._show_startup)
    return app.exec()
