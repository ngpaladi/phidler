"""Unsaved-changes tracking: the title-bar modified marker, and the on-exit
(and on-New/Open) save prompt."""

from __future__ import annotations

import os

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox

from phidler.main_window import MainWindow


def _place(win, line: str) -> None:
    win.console_panel.input.setText(line)
    win.console_panel._on_return()


# -- modified state + title ----------------------------------------------------


def test_fresh_project_is_not_modified(qapp):
    win = MainWindow()
    assert win._is_modified() is False
    assert win.isWindowModified() is False
    assert "Untitled" in win.windowTitle()
    assert "[*]" in win.windowTitle()  # Qt placeholder for the modified marker


def test_an_edit_marks_modified_and_undo_clears_it(qapp):
    win = MainWindow()
    _place(win, "place('straight', length=10.0)")
    assert win._is_modified() is True
    assert win.isWindowModified() is True

    win.undo_stack.undo()
    assert win._is_modified() is False
    assert win.isWindowModified() is False


def test_save_clears_modified_and_titles_the_file(qapp, tmp_path):
    win = MainWindow()
    _place(win, "place('straight', length=10.0)")
    path = str(tmp_path / "proj.phidler")
    assert win._save_project_to(path) is True
    assert win._is_modified() is False
    assert "proj.phidler" in win.windowTitle()

    _place(win, "place('straight', length=5.0, y=20.0)")
    assert win._is_modified() is True  # a change after saving re-dirties


def test_non_undoable_change_marks_modified(qapp):
    win = MainWindow()
    assert win._is_modified() is False
    win._clear_reference_gds()  # not an undo-stack edit, but changes the project
    assert win._is_modified() is True


def test_opening_resets_to_clean(qapp, tmp_path):
    win = MainWindow()
    _place(win, "place('straight', length=10.0)")
    path = str(tmp_path / "p.phidler")
    win._save_project_to(path)
    _place(win, "place('straight', length=5.0, y=20.0)")
    assert win._is_modified() is True

    win._load_project_file(path)
    assert win._is_modified() is False
    assert win.isWindowModified() is False


# -- on-exit save prompt -------------------------------------------------------


def test_close_without_changes_is_allowed(qapp):
    win = MainWindow()
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()  # nothing to save -> closes


def test_close_with_changes_cancel_aborts(qapp, monkeypatch):
    win = MainWindow()
    _place(win, "place('straight', length=10.0)")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted()  # Cancel keeps the window open


def test_close_with_changes_discard_closes(qapp, monkeypatch):
    win = MainWindow()
    _place(win, "place('straight', length=10.0)")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()  # Discard closes without saving


def test_close_with_changes_save_writes_then_closes(qapp, monkeypatch, tmp_path):
    win = MainWindow()
    win.project_path = str(tmp_path / "proj.phidler")  # so Save doesn't prompt for a path
    _place(win, "place('straight', length=10.0)")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Save)
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    assert os.path.exists(win.project_path)  # it saved on the way out


def test_close_save_that_is_cancelled_aborts_close(qapp, monkeypatch):
    """Choosing Save but then cancelling the Save-As dialog must not close."""
    win = MainWindow()
    _place(win, "place('straight', length=10.0)")  # no project_path -> Save-As
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Save)
    monkeypatch.setattr(win, "_save_project", lambda: False)  # user cancelled Save-As
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted()
