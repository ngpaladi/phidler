"""The launch window (recent-project picker) and its routing in MainWindow."""

from phidler.app import main, project_file_arg
from phidler.main_window import MainWindow
from phidler.panels.startup_dialog import StartupDialog


def test_project_file_arg_picks_the_project_by_extension():
    # No project argument -> None (the startup picker path).
    assert project_file_arg(["phidler"]) is None
    assert project_file_arg(["phidler", "-style", "Fusion"]) is None
    # A .phidler or .py argument is recognised, and Qt options/values around it
    # (e.g. `-style Fusion`) are ignored rather than mistaken for the project.
    assert project_file_arg(["phidler", "chip.phidler"]) == "chip.phidler"
    assert project_file_arg(["phidler", "layout.py"]) == "layout.py"
    assert project_file_arg(["phidler", "-style", "Fusion", "chip.phidler"]) == "chip.phidler"


def test_main_fails_fast_on_a_missing_project_file(capsys):
    # A bad path is rejected before any Qt/PDK setup, with a nonzero exit and a
    # clear stderr message — not a GUI error dialog over a blank window.
    rc = main(["phidler", "/no/such/project.phidler"])
    assert rc == 2
    assert "no such project file" in capsys.readouterr().err


def test_startup_dialog_lists_recents_and_sets_recent_choice(qapp, tmp_path):
    a = str(tmp_path / "a.phidler")
    b = str(tmp_path / "b.phidler")
    dlg = StartupDialog([a, b])
    assert dlg.list.count() == 2

    dlg.list.setCurrentRow(1)
    assert dlg.open_button.isEnabled()
    dlg._open_selected()
    assert dlg.choice == ("recent", b)


def test_startup_dialog_new_and_open_choices(qapp):
    dlg = StartupDialog([])  # no recents: list shows a disabled placeholder
    dlg._finish(("new",))
    assert dlg.choice == ("new",)

    dlg2 = StartupDialog([])
    dlg2._finish(("open",))
    assert dlg2.choice == ("open",)


def test_handle_startup_choice_routes_each_option(qapp, monkeypatch):
    win = MainWindow()
    calls = {}
    monkeypatch.setattr(win, "_load_project_file", lambda p: calls.__setitem__("load", p))
    monkeypatch.setattr(win, "_open_project", lambda: calls.__setitem__("open", True))
    monkeypatch.setattr(win, "_new_project", lambda: calls.__setitem__("new", True))

    win._handle_startup_choice(("recent", "/x/y.phidler"))
    assert calls.get("load") == "/x/y.phidler"

    win._handle_startup_choice(("open",))
    assert calls.get("open") is True

    win._handle_startup_choice(None)  # closed without choosing -> new project
    assert calls.get("new") is True


def test_saving_records_a_recent_project(qapp, tmp_path, monkeypatch):
    import phidler.main_window as mw

    recorded = []
    monkeypatch.setattr(mw, "add_recent", lambda p: recorded.append(p))
    win = MainWindow()
    win.document.add_instance("straight", {"length": 5.0, "width": 0.5})

    path = str(tmp_path / "proj.phidler")
    win._save_project_to(path)
    assert recorded == [path]
