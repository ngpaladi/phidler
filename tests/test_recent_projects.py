"""Recent-projects list backing the startup window."""

import os

from PySide6.QtCore import QSettings

from phidler.recent_projects import MAX_RECENT, add_recent, load_recent


def _settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)


def test_add_recent_moves_to_front_and_dedupes(qapp, tmp_path):
    s = _settings(tmp_path)
    a = tmp_path / "a.phidler"; a.write_text("{}")
    b = tmp_path / "b.phidler"; b.write_text("{}")

    add_recent(str(a), s)
    add_recent(str(b), s)
    add_recent(str(a), s)  # re-opening a moves it to the front, no duplicate

    assert load_recent(s) == [os.path.abspath(str(a)), os.path.abspath(str(b))]


def test_load_recent_drops_missing_files(qapp, tmp_path):
    s = _settings(tmp_path)
    present = tmp_path / "present.phidler"; present.write_text("{}")
    add_recent(str(present), s)
    add_recent(str(tmp_path / "gone.phidler"), s)  # never created on disk

    assert load_recent(s) == [os.path.abspath(str(present))]  # missing one filtered out
    assert len(load_recent(s, existing_only=False)) == 2  # still stored


def test_recent_list_is_capped_and_ordered(qapp, tmp_path):
    s = _settings(tmp_path)
    paths = []
    for i in range(MAX_RECENT + 3):
        p = tmp_path / f"p{i}.phidler"; p.write_text("{}")
        paths.append(os.path.abspath(str(p)))
        add_recent(str(p), s)

    recent = load_recent(s)
    assert len(recent) == MAX_RECENT
    assert recent[0] == paths[-1]  # most recent first
    assert paths[0] not in recent  # oldest dropped past the cap
