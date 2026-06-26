from __future__ import annotations

from typing import Any

from PySide6.QtGui import QUndoCommand

from .document import LayoutDocument, Transform
from .placed_instance import ArraySpec


class AddInstanceCommand(QUndoCommand):
    def __init__(
        self,
        document: LayoutDocument,
        scene,
        component_spec: str,
        kwargs: dict[str, Any],
        x: float = 0.0,
        y: float = 0.0,
        rotation: float = 0.0,
        mirror: bool = False,
        text: str | None = None,
    ) -> None:
        super().__init__(text or f"Place {component_spec}")
        self.document = document
        self.scene = scene
        self.component_spec = component_spec
        self.kwargs = kwargs
        self.x, self.y, self.rotation, self.mirror = x, y, rotation, mirror
        self.inst_id: int | None = None
        self._removed_inst = None
        self.error: Exception | None = None

    def redo(self) -> None:
        # Same QUndoStack.push()-inserts-even-when-redo-raises hazard as
        # AddRouteCommand/EditParamsCommand. Built-in catalog components are
        # exhaustively verified placeable (tests/test_scale.py), but
        # user-supplied custom components are not — a broken one must leave
        # inst_id as None rather than letting the exception escape, or a
        # later undo() would call remove_instance(None).
        self.error = None
        try:
            if self._removed_inst is None:
                inst = self.document.add_instance(
                    self.component_spec, self.kwargs, self.x, self.y, self.rotation, self.mirror
                )
                self.inst_id = inst.id
            else:
                self.document.restore_instance(
                    self._removed_inst, Transform(self.x, self.y, self.rotation, self.mirror)
                )
        except Exception as exc:
            self.error = exc
            return
        self.scene.add_instance_item(self.inst_id)

    def undo(self) -> None:
        if self.inst_id is None:
            return
        self._removed_inst = self.document.remove_instance(self.inst_id)
        self.scene.remove_instance_item(self.inst_id)


class DeleteInstanceCommand(QUndoCommand):
    def __init__(self, document: LayoutDocument, scene, inst_id: int, text: str | None = None) -> None:
        super().__init__(text or "Delete instance")
        self.document = document
        self.scene = scene
        self.inst_id = inst_id
        self._removed_inst = None
        self._transform: Transform | None = None

    def redo(self) -> None:
        self._transform = self.document.get_transform(self.inst_id)
        self._removed_inst = self.document.remove_instance(self.inst_id)
        self.scene.remove_instance_item(self.inst_id)

    def undo(self) -> None:
        self.document.restore_instance(self._removed_inst, self._transform)
        self.scene.add_instance_item(self.inst_id)


class MoveInstanceCommand(QUndoCommand):
    """Wraps an already-applied position/rotation/mirror change (Qt's
    built-in drag already moved the item visually) in an undo command so
    the change participates in the undo stack."""

    def __init__(
        self,
        document: LayoutDocument,
        scene,
        inst_id: int,
        old_transform: Transform,
        new_transform: Transform,
        text: str | None = None,
    ) -> None:
        super().__init__(text or "Move")
        self.document = document
        self.scene = scene
        self.inst_id = inst_id
        self.old_transform = old_transform
        self.new_transform = new_transform

    def redo(self) -> None:
        self._apply(self.new_transform)

    def undo(self) -> None:
        self._apply(self.old_transform)

    def _apply(self, transform: Transform) -> None:
        self.document.set_transform(self.inst_id, transform)
        item = self.scene.items_by_inst.get(self.inst_id)
        if item is not None:
            item.apply_transform(transform.x, transform.y, transform.rotation, transform.mirror, transform.mag)
            item.rotation_deg = transform.rotation
            item.mirror = transform.mirror
            item.mag = transform.mag


class EditParamsCommand(QUndoCommand):
    def __init__(
        self,
        document: LayoutDocument,
        scene,
        inst_id: int,
        old_kwargs: dict[str, Any],
        new_kwargs: dict[str, Any],
        text: str | None = None,
    ) -> None:
        super().__init__(text or "Edit parameters")
        self.document = document
        self.scene = scene
        self.inst_id = inst_id
        self.old_kwargs = old_kwargs
        self.new_kwargs = new_kwargs
        self.error: Exception | None = None

    def redo(self) -> None:
        self._apply(self.new_kwargs)

    def undo(self) -> None:
        self._apply(self.old_kwargs)

    def _apply(self, kwargs: dict[str, Any]) -> None:
        # Same QUndoStack.push()-inserts-even-when-redo-raises hazard as
        # AddRouteCommand: update_instance_params builds the new cell before
        # touching the old ref, so a bad kwarg (e.g. an invalid cross_section
        # name) leaves the instance untouched rather than half-deleted — but
        # the exception must still be swallowed here, not left to escape and
        # poison the undo stack.
        self.error = None
        try:
            self.document.update_instance_params(self.inst_id, kwargs)
        except Exception as exc:
            self.error = exc
            return
        self.scene.resync_geometry(self.inst_id)


class SetArrayCommand(QUndoCommand):
    """Turn an instance into (or out of) a rectangular array. resync_geometry
    re-pulls the now-tiled polygons, so the canvas updates the same way a
    parameter edit does."""

    def __init__(
        self,
        document: LayoutDocument,
        scene,
        inst_id: int,
        old_array: ArraySpec,
        new_array: ArraySpec,
        text: str | None = None,
    ) -> None:
        super().__init__(text or "Edit array")
        self.document = document
        self.scene = scene
        self.inst_id = inst_id
        self.old_array = old_array
        self.new_array = new_array

    def redo(self) -> None:
        self._apply(self.new_array)

    def undo(self) -> None:
        self._apply(self.old_array)

    def _apply(self, array: ArraySpec) -> None:
        self.document.set_array(self.inst_id, array)
        self.scene.resync_geometry(self.inst_id)


class AddRouteCommand(QUndoCommand):
    """Routing is deterministic given the same ports/cross_section, so redo
    after an undo just re-runs add_route rather than trying to restore the
    deleted klayout refs (route_single creates them internally; there's no
    standalone cell object to keep around the way there is for instances)."""

    def __init__(
        self,
        document: LayoutDocument,
        scene,
        inst_a_id: int,
        port_a: str,
        inst_b_id: int,
        port_b: str,
        cross_section: str = "strip",
        goal_length_um: float | None = None,
        auto_match: bool = False,
        text: str | None = None,
    ) -> None:
        super().__init__(text or "Add route")
        self.document = document
        self.scene = scene
        self.inst_a_id = inst_a_id
        self.port_a = port_a
        self.inst_b_id = inst_b_id
        self.port_b = port_b
        self.cross_section = cross_section
        self.goal_length_um = goal_length_um
        self.auto_match = auto_match
        self._meander_amplitude_um: float | None = None  # captured so redo rebuilds the same geometry
        self.route_id: int | None = None
        self.error: Exception | None = None

    def redo(self) -> None:
        # QUndoStack.push() still inserts a command onto the stack even if
        # redo() raises, so a routing failure (e.g. incompatible ports) must
        # be swallowed here and left as a no-op rather than propagating —
        # otherwise a later undo() would try to remove a route that was
        # never actually created.
        self.error = None
        try:
            route = self.document.add_route(
                self.inst_a_id,
                self.port_a,
                self.inst_b_id,
                self.port_b,
                self.cross_section,
                route_id=self.route_id,
                goal_length_um=self.goal_length_um,
                auto_match=self.auto_match,
                meander_amplitude_um=self._meander_amplitude_um,
            )
        except Exception as exc:  # surfaced to the caller via .error, not re-raised (see note above)
            self.error = exc
            return
        self.route_id = route.id
        self._meander_amplitude_um = route.meander_amplitude_um  # reuse on the next redo for determinism
        self.scene.add_route_item(self.route_id)

    def undo(self) -> None:
        if self.route_id is None:
            return
        self.document.remove_route(self.route_id)
        self.scene.remove_route_item(self.route_id)


class DeleteRouteCommand(QUndoCommand):
    def __init__(self, document: LayoutDocument, scene, route_id: int, text: str | None = None) -> None:
        super().__init__(text or "Delete route")
        self.document = document
        self.scene = scene
        self.route_id = route_id
        self._saved: tuple[int, str, int, str, str] | None = None
        self.error: Exception | None = None

    def redo(self) -> None:
        route = self.document.routes[self.route_id]
        self._saved = (
            route.instance_id_a,
            route.port_name_a,
            route.instance_id_b,
            route.port_name_b,
            route.cross_section,
        )
        self.document.remove_route(self.route_id)
        self.scene.remove_route_item(self.route_id)

    def undo(self) -> None:
        # If this route's endpoint instance was independently deleted in
        # the meantime (no cascade-delete — see add_route's docstring),
        # restoring it is impossible: leave it removed rather than letting
        # add_route's KeyError/ValueError escape mid-undo and abort
        # whatever macro this is part of.
        self.error = None
        inst_a_id, port_a, inst_b_id, port_b, cross_section = self._saved
        try:
            self.document.add_route(inst_a_id, port_a, inst_b_id, port_b, cross_section, route_id=self.route_id)
        except Exception as exc:
            self.error = exc
            return
        self.scene.add_route_item(self.route_id)
