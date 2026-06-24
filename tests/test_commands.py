import math

from PySide6.QtGui import QUndoStack

from phidler.canvas.scene import LayoutScene
from phidler.model.commands import (
    AddInstanceCommand,
    DeleteInstanceCommand,
    EditParamsCommand,
    MoveInstanceCommand,
)
from phidler.model.document import LayoutDocument, Transform


def _setup():
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    stack = QUndoStack()
    return doc, scene, stack


def test_add_instance_undo_redo(qapp):
    doc, scene, stack = _setup()
    cmd = AddInstanceCommand(doc, scene, "straight", {"length": 10.0, "width": 0.5})
    stack.push(cmd)
    inst_id = cmd.inst_id
    assert inst_id in doc.instances
    assert inst_id in scene.items_by_inst

    stack.undo()
    assert inst_id not in doc.instances
    assert inst_id not in scene.items_by_inst

    stack.redo()
    assert inst_id in doc.instances
    assert inst_id in scene.items_by_inst
    # redo must reuse the same instance id and restore its transform
    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, 0.0) and math.isclose(t.y, 0.0)


def test_add_instance_failure_does_not_corrupt_state(qapp):
    """Custom user-supplied components (unlike the exhaustively-verified
    built-in catalog) can genuinely fail to construct. AddInstanceCommand
    must record the failure on .error and leave inst_id as None rather than
    let the exception escape — QUndoStack.push() still inserts a command
    even when its redo() raises (confirmed elsewhere in this codebase), so
    an unguarded failure would leave a poisoned entry whose later undo()
    calls remove_instance(None) and crashes."""
    doc, scene, stack = _setup()
    cmd = AddInstanceCommand(doc, scene, "definitely_not_a_real_component", {})
    stack.push(cmd)

    assert cmd.error is not None
    assert cmd.inst_id is None
    assert len(doc.instances) == 0

    stack.undo()  # must not raise despite the poisoned entry on the stack
    stack.redo()
    assert len(doc.instances) == 0


def test_delete_instance_undo_redo(qapp):
    doc, scene, stack = _setup()
    add_cmd = AddInstanceCommand(doc, scene, "straight", {"length": 10.0, "width": 0.5}, x=3.0, y=4.0)
    stack.push(add_cmd)
    inst_id = add_cmd.inst_id

    stack.push(DeleteInstanceCommand(doc, scene, inst_id))
    assert inst_id not in doc.instances

    stack.undo()
    assert inst_id in doc.instances
    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, 3.0) and math.isclose(t.y, 4.0)


def test_move_instance_undo_redo(qapp):
    doc, scene, stack = _setup()
    add_cmd = AddInstanceCommand(doc, scene, "straight", {"length": 10.0, "width": 0.5})
    stack.push(add_cmd)
    inst_id = add_cmd.inst_id

    old_t = doc.get_transform(inst_id)
    new_t = Transform(x=5.0, y=2.0, rotation=90.0, mirror=False)
    stack.push(MoveInstanceCommand(doc, scene, inst_id, old_t, new_t))

    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, 5.0) and math.isclose(t.rotation, 90.0)

    stack.undo()
    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, old_t.x) and math.isclose(t.rotation, old_t.rotation)

    stack.redo()
    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, 5.0) and math.isclose(t.rotation, 90.0)


def test_edit_params_undo_redo_preserves_transform(qapp):
    doc, scene, stack = _setup()
    add_cmd = AddInstanceCommand(doc, scene, "straight", {"length": 10.0, "width": 0.5}, x=1.0, y=1.0)
    stack.push(add_cmd)
    inst_id = add_cmd.inst_id

    old_kwargs = dict(doc.instances[inst_id].kwargs)
    new_kwargs = {"length": 20.0, "width": 0.5}
    stack.push(EditParamsCommand(doc, scene, inst_id, old_kwargs, new_kwargs))

    assert doc.instances[inst_id].kwargs["length"] == 20.0
    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, 1.0) and math.isclose(t.y, 1.0)
    shapes = doc.get_polygons_for_instance(inst_id)
    xs = [x for hull, _holes in shapes[(1, 0)] for x, _ in hull]
    assert math.isclose(max(xs), 20.0, abs_tol=1e-6)

    stack.undo()
    assert doc.instances[inst_id].kwargs["length"] == 10.0
    t = doc.get_transform(inst_id)
    assert math.isclose(t.x, 1.0) and math.isclose(t.y, 1.0)
