from __future__ import annotations

import itertools
from dataclasses import dataclass

import gdsfactory as gf
import klayout.db as kdb

from .layers import LayerInfo, LayerKey, layer_info_for
from .placed_instance import PlacedInstance, PlacedRoute

Point = tuple[float, float]
Shape = tuple[list[Point], list[list[Point]]]  # (hull points, [hole points, ...])
ShapesByLayer = dict[LayerKey, list[Shape]]


def _shapes_from_polygons(polygons_by_layer, dbu: float, layers: dict[LayerKey, LayerInfo]) -> ShapesByLayer:
    result: ShapesByLayer = {}
    for layer_key, polys in polygons_by_layer.items():
        key = tuple(layer_key)
        layer_info_for(key, layers)
        shapes: list[Shape] = []
        for p in polys:
            dpoly = p.to_dtype(dbu)
            hull = [(pt.x, pt.y) for pt in dpoly.each_point_hull()]
            holes = [[(pt.x, pt.y) for pt in dpoly.each_point_hole(i)] for i in range(dpoly.holes())]
            shapes.append((hull, holes))
        result[key] = shapes
    return result


def shapes_for_cell(cell: gf.Component) -> ShapesByLayer:
    """Layer-registry-free variant of LayoutDocument._shapes_for_cell, for
    contexts (e.g. a palette hover preview) that just need a component's
    rendered shapes without a live document to register layer names into."""
    return _shapes_from_polygons(cell.get_polygons(by="tuple"), cell.kcl.dbu, {})


@dataclass
class Transform:
    x: float
    y: float
    rotation: float
    mirror: bool
    mag: float = 1.0  # uniform geometric scale; ports/geometry scale with it


@dataclass
class ProjectSettings:
    """Project-level metadata captured by the New Project dialog (material
    platform, thickness, wavelength) and the waveguide width this implies
    via waveguide_calc.py. Doesn't change the active PDK or any geometry —
    it's metadata plus a default cross_section/width suggestion, persisted
    so a reopened project remembers what platform it was designed for."""

    platform_name: str = "Silicon (SOI)"
    core_index: float = 3.45
    clad_index: float = 1.44
    thickness_um: float = 0.220
    # Generic default (2µm), not sourced from any specific foundry process —
    # a wafer/process choice independent of which platform/material is
    # selected, so platform-preset switching does not touch this field.
    # Doesn't affect the EIM width estimate (which assumes semi-infinite
    # cladding) but is the real domain extent for the FDTD/mode-solver
    # vertical stack.
    clad_thickness_um: float = 2.0
    wavelength_um: float = 1.55
    cross_section: str = "strip"


class LayoutDocument:
    """Owns the single gdsfactory top cell being edited, plus the bookkeeping
    (instance/route records, layer list) needed to render, undo, and export it.

    Geometry calls (get_component, get_polygons) only happen on
    place/edit/import — never on every interactive move — so dragging in the
    view never re-touches gdsfactory until the move is committed.
    """

    def __init__(self) -> None:
        self.top: gf.Component = gf.Component()
        self.instances: dict[int, PlacedInstance] = {}
        self.routes: dict[int, PlacedRoute] = {}
        # Starts empty rather than pre-seeded with the active PDK's full ~47
        # layer map: layer_info_for() (called wherever geometry is actually
        # extracted) adds an entry the first time a layer is encountered,
        # so the Layers panel only ever shows layers genuinely used in the
        # current design instead of the PDK's entire theoretical layer set.
        self.layers: dict[LayerKey, LayerInfo] = {}
        self.reference: gf.Component | None = None
        self.reference_path: str | None = None
        # Paths of custom-component Python files imported this session
        # (see custom_components.py). Custom cells only exist in the
        # active PDK's registry because that import ran *this process* —
        # a saved project that placed one would otherwise fail to reload
        # in a fresh process, since gf.get_component(name) would no
        # longer resolve it. project_io replays these before instances.
        self.custom_component_paths: list[str] = []
        self.project_settings: ProjectSettings = ProjectSettings()
        self._ids = itertools.count(1)

    def record_custom_component_path(self, path: str) -> None:
        if path not in self.custom_component_paths:
            self.custom_component_paths.append(path)

    def next_id(self) -> int:
        return next(self._ids)

    def bump_id_counter(self, min_next: int) -> None:
        """Used after loading records with explicit ids (add_instance/
        add_route with inst_id=/route_id= bypass next_id() entirely), so a
        freshly-loaded project's next placement doesn't collide with an id
        that was just loaded from disk."""
        self._ids = itertools.count(max(min_next, next(self._ids)))

    # -- instances ---------------------------------------------------------

    def add_instance(
        self,
        component_spec: str,
        kwargs: dict | None = None,
        x: float = 0.0,
        y: float = 0.0,
        rotation: float = 0.0,
        mirror: bool = False,
        mag: float = 1.0,
        inst_id: int | None = None,
    ) -> PlacedInstance:
        kwargs = dict(kwargs or {})
        cell = gf.get_component(component_spec, **kwargs)
        ref = self.top.add_ref(cell)
        ref.dcplx_trans = kdb.DCplxTrans(mag, rotation, mirror, x, y)
        inst = PlacedInstance(
            id=inst_id if inst_id is not None else self.next_id(),
            component_spec=component_spec,
            kwargs=kwargs,
            cell=cell,
            ref=ref,
        )
        self.instances[inst.id] = inst
        for layer_key in cell.layers:
            layer_info_for(tuple(layer_key), self.layers)
        return inst

    def remove_instance(self, inst_id: int) -> PlacedInstance:
        inst = self.instances.pop(inst_id)
        inst.ref.delete()
        return inst

    def restore_instance(self, inst: PlacedInstance, transform: Transform) -> None:
        """Re-insert a previously-removed instance (used by undo)."""
        ref = self.top.add_ref(inst.cell)
        ref.dcplx_trans = kdb.DCplxTrans(transform.mag, transform.rotation, transform.mirror, transform.x, transform.y)
        inst.ref = ref
        self.instances[inst.id] = inst

    def update_instance_params(self, inst_id: int, kwargs: dict) -> PlacedInstance:
        """Regenerate an instance's cell with new kwargs, preserving its
        transform (gdsfactory cells are cached/immutable by kwargs, so an
        edit is delete-old-ref + add-new-ref rather than an in-place mutation).

        Builds the new cell BEFORE deleting the old ref: get_component(...)
        raises on bad kwargs (e.g. an invalid cross_section name), and if
        that happened after the delete, the instance would be left
        half-deleted — gone from the GDS topology but still tracked as if
        it existed, with a now-unusable ref (confirmed empirically: this
        exact ordering bug used to leave `top.insts` missing the instance
        while `document.instances` still claimed it was there)."""
        inst = self.instances[inst_id]
        transform = self.get_transform(inst_id)
        kwargs = dict(kwargs)
        cell = gf.get_component(inst.component_spec, **kwargs)
        inst.ref.delete()
        ref = self.top.add_ref(cell)
        ref.dcplx_trans = kdb.DCplxTrans(transform.mag, transform.rotation, transform.mirror, transform.x, transform.y)
        inst.cell = cell
        inst.ref = ref
        inst.kwargs = kwargs
        for layer_key in cell.layers:
            layer_info_for(tuple(layer_key), self.layers)
        return inst

    def set_transform(self, inst_id: int, transform: Transform) -> None:
        inst = self.instances[inst_id]
        inst.ref.dcplx_trans = kdb.DCplxTrans(transform.mag, transform.rotation, transform.mirror, transform.x, transform.y)

    def get_transform(self, inst_id: int) -> Transform:
        t = self.instances[inst_id].ref.dcplx_trans
        return Transform(x=t.disp.x, y=t.disp.y, rotation=t.angle, mirror=t.is_mirror(), mag=t.mag)

    def get_polygons_for_instance(self, inst_id: int) -> ShapesByLayer:
        """Shapes in the instance's local (unplaced) coordinate frame. Each
        shape is (hull, holes) — holes must be carried through (not just the
        hull) or the canvas would render a solid fill where the exported GDS
        actually has a cut-out, e.g. for true annuli built via boolean ops."""
        return self._shapes_for_cell(self.instances[inst_id].cell)

    def _shapes_for_cell(self, cell: gf.Component) -> ShapesByLayer:
        return _shapes_from_polygons(cell.get_polygons(by="tuple"), cell.kcl.dbu, self.layers)

    def get_ports_for_instance(self, inst_id: int) -> list[tuple[str, float, float, float, float]]:
        """Local-frame ports as (name, x, y, orientation_deg, width)."""
        cell = self.instances[inst_id].cell
        return [(p.name, p.center[0], p.center[1], p.orientation, p.width) for p in cell.ports]

    def get_absolute_ports_for_instance(self, inst_id: int) -> list[tuple[str, float, float]]:
        """Port positions in the document's absolute (top-cell) frame —
        local ports transformed by the instance's current placement."""
        t = self.get_transform(inst_id)
        return self.get_absolute_ports_for_transform(inst_id, t.x, t.y, t.rotation, t.mirror, t.mag)

    def get_absolute_ports_for_transform(
        self, inst_id: int, x: float, y: float, rotation: float, mirror: bool, mag: float
    ) -> list[tuple[str, float, float]]:
        """Like get_absolute_ports_for_instance, but against a supplied
        transform rather than the instance's currently-stored one — needed
        while dragging, when the Qt item's on-screen position has already
        moved but the document hasn't been updated yet (used for
        port-to-port snapping). Reuses kdb.DCplxTrans, the same transform
        primitive this document already uses for geometry, rather than
        re-deriving the mag/rotation/mirror composition order for ports."""
        transform = kdb.DCplxTrans(mag, rotation, mirror, x, y)
        result = []
        for name, lx, ly, _orientation, _width in self.get_ports_for_instance(inst_id):
            p = transform.trans(kdb.DPoint(lx, ly))
            result.append((name, p.x, p.y))
        return result

    # -- routes ----------------------------------------------------------

    def add_route(
        self,
        inst_a_id: int,
        port_a: str,
        inst_b_id: int,
        port_b: str,
        cross_section: str = "strip",
        route_id: int | None = None,
    ) -> PlacedRoute:
        # There's no cascade-delete: removing an instance doesn't remove
        # routes that referenced it, so a route can outlive its endpoint
        # (e.g. delete just the instance, leave the route; or undo a
        # same-macro instance+route delete in the wrong order). Raise a
        # clear, specific error here rather than a bare KeyError from the
        # dict lookup, since both AddRouteCommand and DeleteRouteCommand.undo
        # need to distinguish "this failed because the endpoint is gone"
        # from any other routing failure.
        for inst_id in (inst_a_id, inst_b_id):
            if inst_id not in self.instances:
                raise ValueError(f"Cannot route: instance #{inst_id} no longer exists")
        p1 = self.instances[inst_a_id].ref.ports[port_a]
        p2 = self.instances[inst_b_id].ref.ports[port_b]
        route = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section)
        placed = PlacedRoute(
            id=route_id if route_id is not None else self.next_id(),
            instance_id_a=inst_a_id,
            port_name_a=port_a,
            instance_id_b=inst_b_id,
            port_name_b=port_b,
            cross_section=cross_section,
            refs=list(route.instances),
            length=route.length,
        )
        self.routes[placed.id] = placed
        for ref in placed.refs:
            for layer_key in self._shapes_for_ref(ref):
                layer_info_for(layer_key, self.layers)
        return placed

    def remove_route(self, route_id: int) -> PlacedRoute:
        route = self.routes.pop(route_id)
        for ref in route.refs:
            ref.delete()
        return route

    def get_shapes_for_route(self, route_id: int) -> ShapesByLayer:
        """Shapes already in absolute (top-cell) coordinates — routes are
        rendered without any extra Qt-side transform, unlike instances."""
        combined: ShapesByLayer = {}
        for ref in self.routes[route_id].refs:
            for key, shapes in self._shapes_for_ref(ref).items():
                combined.setdefault(key, []).extend(shapes)
        return combined

    def _shapes_for_ref(self, ref) -> ShapesByLayer:
        from gdsfactory.functions import get_polygons

        return _shapes_from_polygons(get_polygons(ref, by="tuple"), self.top.kcl.dbu, self.layers)

    # -- reference (background) GDS -----------------------------------------

    def import_reference(self, path: str) -> None:
        """Loads a GDS as a read-only backdrop to design against (e.g. a
        foundry floorplan). Deliberately kept as a standalone Component,
        never added as a ref into self.top — otherwise it would silently
        get re-emitted into the user's own GDS export."""
        self.reference = gf.import_gds(path)
        self.reference_path = path

    def clear_reference(self) -> None:
        self.reference = None
        self.reference_path = None

    def get_shapes_for_reference(self) -> ShapesByLayer:
        if self.reference is None:
            return {}
        return self._shapes_for_cell(self.reference)

    # -- whole-document reset (File > New, or before loading a project) -----

    def clear_all(self) -> tuple[list[int], list[int]]:
        """Removes every instance and route. Returns (removed_instance_ids,
        removed_route_ids) so the caller (which also owns the Qt scene
        items) can remove the matching graphics items."""
        removed_instance_ids = list(self.instances)
        removed_route_ids = list(self.routes)
        for route_id in removed_route_ids:
            self.remove_route(route_id)
        for inst_id in removed_instance_ids:
            self.remove_instance(inst_id)
        self.clear_reference()
        self.custom_component_paths = []
        return removed_instance_ids, removed_route_ids

    # -- export --------------------------------------------------------------

    def export_gds(self, path: str) -> str:
        return str(self.top.write_gds(path))
