from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

# Command-line project files are recognised by extension — the same two the
# Open dialog accepts (.phidler native projects, .py generated scripts). Keying
# off the suffix (rather than "first non-flag arg") lets Qt's own options and
# their values, e.g. `-style Fusion`, pass through to QApplication untouched.
_PROJECT_SUFFIXES = (".phidler", ".py")


def project_file_arg(argv: list[str]) -> str | None:
    """The optional project file to open on launch, if one was given on the
    command line (e.g. `run.sh myproject.phidler`): the first argument whose
    name looks like a project file. None when none was passed — then the
    startup picker is shown as before."""
    return next((a for a in argv[1:] if a.endswith(_PROJECT_SUFFIXES)), None)


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
    argv = list(sys.argv if argv is None else argv)

    # A project file may be given on the command line to open straight into it,
    # skipping the startup picker. Validate it up front (before the slow PDK
    # activation and any Qt setup) so a typo fails fast with a clear message
    # instead of surfacing later as a GUI error dialog over a blank window.
    project_arg = project_file_arg(argv)
    if project_arg is not None and not os.path.isfile(project_arg):
        print(f"phidler: no such project file: {project_arg}", file=sys.stderr)
        return 2

    activate_pdk()

    app = QApplication(argv)
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
    # for a real user to dismiss it with). Opening a command-line file is
    # deferred for the same reason (it may itself pop an error dialog).
    if project_arg is not None:
        QTimer.singleShot(0, lambda: window._load_project_file(project_arg))
    else:
        QTimer.singleShot(0, window._show_startup)
    return app.exec()
