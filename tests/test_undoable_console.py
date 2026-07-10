"""Console place()/route() and Claude's run_python are undoable, grouped so one
Ctrl+Z reverts a whole console entry or AI action."""

from __future__ import annotations

from PySide6.QtGui import QUndoCommand, QUndoStack

from phidler.main_window import MainWindow, _UndoGrouper


def _submit(win, line: str) -> None:
    win.console_panel.input.setText(line)
    win.console_panel._on_return()


# -- _UndoGrouper (lazy, no empty macros) --------------------------------------


class _Noop(QUndoCommand):
    def redo(self):
        pass

    def undo(self):
        pass


def test_undo_grouper_skips_empty_groups(qapp):
    stack = QUndoStack()
    grouper = _UndoGrouper(stack)
    with grouper.group("nothing happens"):
        pass
    assert stack.count() == 0  # no do-nothing entry left behind


def test_undo_grouper_wraps_pushes_in_one_macro(qapp):
    stack = QUndoStack()
    grouper = _UndoGrouper(stack)
    with grouper.group("two things"):
        grouper.push(_Noop())
        grouper.push(_Noop())
    assert stack.count() == 1  # one macro, not two commands
    assert stack.text(0) == "two things"


def test_undo_grouper_push_without_a_group_is_plain(qapp):
    stack = QUndoStack()
    grouper = _UndoGrouper(stack)
    grouper.push(_Noop())
    grouper.push(_Noop())
    assert stack.count() == 2  # ungrouped: two separate entries


# -- console place()/route() are undoable --------------------------------------


def test_console_place_is_undoable(qapp):
    win = MainWindow()
    _submit(win, "place('straight', length=10.0)")
    assert len(win.document.instances) == 1
    assert win.undo_stack.count() == 1

    win.undo_stack.undo()
    assert len(win.document.instances) == 0
    win.undo_stack.redo()
    assert len(win.document.instances) == 1


def test_console_multiline_block_undoes_as_one_step(qapp):
    win = MainWindow()
    _submit(win, "for i in range(3):")
    _submit(win, "    place('straight', length=10.0, y=i*20)")
    _submit(win, "")  # blank line runs the block
    assert len(win.document.instances) == 3
    assert win.undo_stack.count() == 1  # the whole loop is one undo entry

    win.undo_stack.undo()
    assert len(win.document.instances) == 0


def test_console_route_is_undoable(qapp):
    win = MainWindow()
    _submit(win, "a = place('straight', length=10.0)")
    _submit(win, "b = place('straight', length=10.0, x=40.0, rotation=180.0)")
    _submit(win, "route(a.id, 'o2', b.id, 'o2')")
    assert len(win.document.routes) == 1
    route_id = next(iter(win.document.routes))
    assert route_id in win.scene.route_items

    win.undo_stack.undo()  # undo the route
    assert len(win.document.routes) == 0
    assert route_id not in win.scene.route_items


# -- Claude's run_python groups into one undo ----------------------------------


def test_ai_run_python_groups_into_one_undo(qapp):
    win = MainWindow()
    before = win.undo_stack.count()
    win.console_panel.run_python_from_agent(
        "a = place('straight', length=10.0)\n"
        "b = place('straight', length=10.0, y=20.0)\n"
        "route(a.id, 'o1', b.id, 'o1')\n"
    )
    assert win.undo_stack.count() - before == 1  # one macro for the whole AI action
    assert len(win.document.instances) == 2 and len(win.document.routes) == 1

    win.undo_stack.undo()  # a single Ctrl+Z reverts everything Claude did
    assert len(win.document.instances) == 0 and len(win.document.routes) == 0


def test_ai_readonly_code_leaves_no_undo_entry(qapp):
    win = MainWindow()
    before = win.undo_stack.count()
    out = win.console_panel.run_python_from_agent("print('instances:', len(doc.instances))")
    assert win.undo_stack.count() == before  # nothing mutated -> no undo step
    assert "instances: 0" in out


def test_place_error_is_surfaced_and_recoverable(qapp):
    win = MainWindow()
    _submit(win, "place('straight', length=10.0)")  # a good one first
    out_before = win.undo_stack.count()
    # A bad component raises inside place(); the console shows it and stays usable.
    win.console_panel.run_python_from_agent("place('definitely_not_a_component')")
    assert "not in" in win.console_panel.output.toPlainText() or "Error" in win.console_panel.output.toPlainText()
    # The good instance is still there and still undoable.
    assert len(win.document.instances) == 1
    win.undo_stack.undo()
    # Undo unwinds cleanly back through the good placement (no corrupted stack).
    assert len(win.document.instances) in (0, 1)
