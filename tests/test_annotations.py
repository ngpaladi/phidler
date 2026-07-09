"""Notes (pinned text comments) and their callout drawings (rect/arrow):
the model, undo commands, canvas interaction, persistence, and window wiring."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtTest import QTest

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.model.annotation import Annotation, CalloutShape
from phidler.model.commands import (
    AddAnnotationCommand,
    AddCalloutCommand,
    DeleteAnnotationCommand,
    EditAnnotationTextCommand,
    MoveAnnotationCommand,
)
from phidler.model.document import LayoutDocument
from phidler.project_io import load_project, save_project


# -- model ------------------------------------------------------------------


def test_document_annotation_crud():
    doc = LayoutDocument()
    ann = doc.add_annotation("hello", 1.0, 2.0)
    assert doc.annotations[ann.id] is ann
    assert (ann.x, ann.y, ann.text) == (1.0, 2.0, "hello")

    doc.set_annotation_position(ann.id, 3.0, 4.0)
    doc.set_annotation_text(ann.id, "world")
    assert (ann.x, ann.y, ann.text) == (3.0, 4.0, "world")

    shape = CalloutShape("rect", [(-1, -1), (2, 2)])
    doc.add_annotation_shape(ann.id, shape)
    assert ann.shapes == [shape]
    doc.remove_annotation_shape(ann.id, shape)
    assert ann.shapes == []

    removed = doc.remove_annotation(ann.id)
    assert removed is ann and ann.id not in doc.annotations
    doc.restore_annotation(removed)
    assert doc.annotations[ann.id] is ann


def test_annotation_ids_share_the_instance_route_counter():
    doc = LayoutDocument()
    inst = doc.add_instance("straight", {"length": 5.0, "width": 0.5})
    ann = doc.add_annotation("n", 0.0, 0.0)
    assert ann.id != inst.id  # unique across families


def test_clear_all_drops_annotations():
    doc = LayoutDocument()
    doc.add_annotation("n", 0.0, 0.0)
    doc.clear_all()
    assert doc.annotations == {}


# -- commands (undo/redo) ---------------------------------------------------


def _doc_scene_undo():
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    return doc, scene, QUndoStack()


def test_add_annotation_command_roundtrips_and_keeps_id_on_redo(qapp):
    doc, scene, undo = _doc_scene_undo()
    cmd = AddAnnotationCommand(doc, scene, "note", 10.0, 5.0)
    undo.push(cmd)
    ann_id = cmd.ann_id
    assert ann_id in doc.annotations and ann_id in scene.annotation_items

    undo.undo()
    assert ann_id not in doc.annotations and ann_id not in scene.annotation_items

    undo.redo()
    assert cmd.ann_id == ann_id  # same id reused, not a fresh one
    assert ann_id in doc.annotations and ann_id in scene.annotation_items


def test_delete_annotation_command_restores_shapes_on_undo(qapp):
    doc, scene, undo = _doc_scene_undo()
    add = AddAnnotationCommand(doc, scene, "n", 0.0, 0.0)
    undo.push(add)
    ann_id = add.ann_id
    undo.push(AddCalloutCommand(doc, scene, ann_id, CalloutShape("arrow", [(0, 0), (5, 5)])))

    undo.push(DeleteAnnotationCommand(doc, scene, ann_id))
    assert ann_id not in doc.annotations and ann_id not in scene.annotation_items

    undo.undo()  # the note comes back with its callout intact
    assert ann_id in doc.annotations
    assert [s.kind for s in doc.annotations[ann_id].shapes] == ["arrow"]


def test_move_and_edit_annotation_commands_are_undoable(qapp):
    doc, scene, undo = _doc_scene_undo()
    add = AddAnnotationCommand(doc, scene, "old", 1.0, 1.0)
    undo.push(add)
    ann_id = add.ann_id

    undo.push(MoveAnnotationCommand(doc, scene, ann_id, 1.0, 1.0, 7.0, 8.0))
    undo.push(EditAnnotationTextCommand(doc, scene, ann_id, "old", "new"))
    assert (doc.annotations[ann_id].x, doc.annotations[ann_id].y) == (7.0, 8.0)
    assert doc.annotations[ann_id].text == "new"

    undo.undo()  # edit
    undo.undo()  # move
    assert doc.annotations[ann_id].text == "old"
    assert (doc.annotations[ann_id].x, doc.annotations[ann_id].y) == (1.0, 1.0)


def test_add_callout_command_undo_removes_only_that_shape(qapp):
    doc, scene, undo = _doc_scene_undo()
    add = AddAnnotationCommand(doc, scene, "n", 0.0, 0.0)
    undo.push(add)
    ann_id = add.ann_id
    undo.push(AddCalloutCommand(doc, scene, ann_id, CalloutShape("rect", [(0, 0), (1, 1)])))
    undo.push(AddCalloutCommand(doc, scene, ann_id, CalloutShape("arrow", [(0, 0), (2, 2)])))
    assert len(doc.annotations[ann_id].shapes) == 2

    undo.undo()  # drops only the arrow
    assert [s.kind for s in doc.annotations[ann_id].shapes] == ["rect"]


# -- persistence ------------------------------------------------------------


def test_save_load_round_trips_notes_and_callouts(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    ann = doc.add_annotation("check gap", 12.0, -3.0, color="#ff0000")
    doc.add_annotation_shape(ann.id, CalloutShape("rect", [(-2, -2), (4, 4)]))
    doc.add_annotation_shape(ann.id, CalloutShape("arrow", [(0, 0), (6, 1)]))

    path = str(tmp_path / "notes.phidler")
    save_project(doc, path)

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_project(path, doc2, scene2)

    assert len(doc2.annotations) == 1
    a2 = doc2.annotations[ann.id]
    assert (a2.text, a2.x, a2.y, a2.color) == ("check gap", 12.0, -3.0, "#ff0000")
    assert [s.kind for s in a2.shapes] == ["rect", "arrow"]
    assert a2.shapes[0].points == [(-2, -2), (4, 4)]  # tuples restored, not lists
    assert ann.id in scene2.annotation_items


def test_load_project_without_annotations_key_is_fine(qapp, tmp_path):
    """Projects saved before notes existed have no 'annotations' key; loading
    one must not fail and must leave the document note-free."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    doc.add_instance("straight", {"length": 5.0, "width": 0.5})
    path = str(tmp_path / "old.phidler")
    save_project(doc, path)

    import json

    data = json.loads((tmp_path / "old.phidler").read_text())
    del data["annotations"]
    (tmp_path / "old.phidler").write_text(json.dumps(data))

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_project(path, doc2, scene2)  # must not raise
    assert doc2.annotations == {}


def test_loading_a_project_clears_previous_annotation_items(qapp, tmp_path):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    doc.add_annotation("stale", 0.0, 0.0)
    scene.add_annotation_item(next(iter(doc.annotations)))

    empty = LayoutDocument()
    path = str(tmp_path / "empty.phidler")
    save_project(empty, path)
    load_project(path, doc, scene)
    assert scene.annotation_items == {}


# -- view interaction -------------------------------------------------------


def _view(doc=None):
    doc = doc or LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene, undo_stack=QUndoStack())
    view.resize(400, 400)
    view.show()
    return doc, scene, view


def test_note_mode_click_emits_note_request_at_scene_point(qapp):
    _doc, _scene, view = _view()
    got = []
    view.note_requested.connect(lambda x, y: got.append((x, y)))

    view.set_annotate_mode("note")
    view.snap_enabled = False
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(QPointF(6.0, 7.0)))

    assert len(got) == 1
    assert abs(got[0][0] - 6.0) < 1e-6 and abs(got[0][1] - 7.0) < 1e-6


def test_callout_drag_emits_callout_request_with_kind_and_endpoints(qapp):
    _doc, _scene, view = _view()
    got = []
    view.callout_requested.connect(lambda *a: got.append(a))

    view.set_annotate_mode("box")
    view.snap_enabled = False
    start, end = QPointF(2.0, 2.0), QPointF(9.0, 5.0)
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(start))
    QTest.mouseMove(view.viewport(), view.mapFromScene(end))
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(end))

    assert len(got) == 1
    kind, x0, y0, x1, y1 = got[0]
    assert kind == "box"
    assert abs(x0 - 2.0) < 1e-6 and abs(y1 - 5.0) < 1e-6


def test_zero_size_callout_drag_is_ignored(qapp):
    _doc, _scene, view = _view()
    got = []
    view.callout_requested.connect(lambda *a: got.append(a))
    view.set_annotate_mode("arrow")

    pt = view.mapFromScene(QPointF(3.0, 3.0))
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, pt)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, pt)
    assert got == []  # a click with no drag points at nothing


def test_double_click_a_note_emits_edit_request(qapp):
    doc, scene, view = _view()
    ann = doc.add_annotation("edit me", 0.0, 0.0)
    scene.add_annotation_item(ann.id)
    got = []
    view.annotation_edit_requested.connect(got.append)

    QTest.mouseDClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(QPointF(0.0, 0.0)))
    assert got == [ann.id]


def test_annotate_mode_is_mutually_exclusive_with_other_tools(qapp):
    _doc, scene, view = _view()

    view.set_annotate_mode("note")
    assert view.annotate_mode == "note"

    view.set_measure_mode(True)  # entering measure clears annotate
    assert view.annotate_mode == ""
    assert view.measure_mode is True

    view.set_annotate_mode("box")  # entering annotate clears measure
    assert view.measure_mode is False
    assert view.annotate_mode == "box"


def test_escape_exits_annotate_mode(qapp):
    _doc, _scene, view = _view()
    view.set_annotate_mode("note")
    assert view.cancel_current_action() is True
    assert view.annotate_mode == ""


# -- window wiring ----------------------------------------------------------


def test_main_window_note_action_places_a_note(qapp, monkeypatch):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.view.resize(400, 400)
    win.show()

    monkeypatch.setattr(
        "phidler.main_window.QInputDialog.getMultiLineText",
        staticmethod(lambda *a, **k: ("a comment", True)),
    )
    win.note_action.setChecked(True)
    assert win.view.annotate_mode == "note"
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.mapFromScene(QPointF(0.0, 0.0)))

    assert len(win.document.annotations) == 1
    assert next(iter(win.document.annotations.values())).text == "a comment"


def test_callout_attaches_to_the_selected_note(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    ann = win.document.add_annotation("n", 0.0, 0.0)
    item = win.scene.add_annotation_item(ann.id)
    item.setSelected(True)

    win._on_callout_requested("rect", 0.0, 0.0, 5.0, 5.0)
    assert len(win.document.annotations[ann.id].shapes) == 1
    shape = win.document.annotations[ann.id].shapes[0]
    assert shape.kind == "rect"
    assert shape.points == [(0.0, 0.0), (5.0, 5.0)]  # relative to the pin at (0,0)


def test_callout_without_a_single_selected_note_is_a_no_op(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    ann = win.document.add_annotation("n", 0.0, 0.0)
    win.scene.add_annotation_item(ann.id)  # present but not selected

    win._on_callout_requested("arrow", 0.0, 0.0, 3.0, 3.0)
    assert win.document.annotations[ann.id].shapes == []


def test_delete_selected_removes_notes_too(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    ann = win.document.add_annotation("n", 0.0, 0.0)
    item = win.scene.add_annotation_item(ann.id)
    item.setSelected(True)

    win._delete_selected()
    assert ann.id not in win.document.annotations
    assert ann.id not in win.scene.annotation_items

    win.undo_stack.undo()
    assert ann.id in win.document.annotations  # delete is undoable


def test_annotate_toolbar_buttons_are_mutually_exclusive(qapp):
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.note_action.setChecked(True)
    assert win.view.annotate_mode == "note"

    win.callout_box_action.setChecked(True)
    assert win.view.annotate_mode == "box"
    assert win.note_action.isChecked() is False  # kept in sync by _on_annotate_mode_changed

    win.callout_box_action.setChecked(False)
    assert win.view.annotate_mode == ""
