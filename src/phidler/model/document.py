from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

import gdsfactory as gf
import klayout.db as kdb

from .annotation import DEFAULT_ANNOTATION_COLOR, Annotation, CalloutShape
from .layers import LayerInfo, LayerKey, layer_info_for
from .placed_instance import ArraySpec, PlacedInstance, PlacedRoute

if TYPE_CHECKING:
    # Imported lazily (type-checking only) to avoid a runtime import cycle:
    # fdtd_sim imports this module, so it can't be imported here at module
    # scope. The field is plain data (set by project_io / the FDTD window,
    # both of which import the real class), so document.py never needs it live.
    from ..fdtd_sim import SimulationConfig

Point = tuple[float, float]
Shape = tuple[list[Point], list[list[Point]]]  # (hull points, [hole points, ...])
ShapesByLayer = dict[LayerKey, list[Shape]]

# Process-global so array-wrapper cell names never collide across documents
# (tests build many documents in one process); a fresh name per wrapper also
# avoids gdsfactory's name-conflict warning when two arrays differ only by
# pitch/counts.
_array_wrapper_counter = itertools.count()

# Automatic length-matching meander bounds (µm). The minimum is bend-radius
# limited — smaller bumps fail to close with euler bends; the search treats
# anything below it as unrealizable. Tolerance is the convergence target.
_MEANDER_MIN_AMPLITUDE_UM = 5.0
_MEANDER_MAX_AMPLITUDE_UM = 5000.0
_MEANDER_TOLERANCE_UM = 0.5


def _meander_steps(p1, p2, amplitude_um: float) -> list[dict]:
    """route_single `steps` for a single perpendicular detour of the given
    amplitude between two ports: bump perpendicular to the dominant separation
    axis, traverse halfway, bump back. Pure (no document state) so the exporter
    can reproduce the exact detour from stored amplitude + live port positions.

    The bump goes *outward* — away from the centerline the route sits on (the
    sign of the segment's midpoint) — so paired arms (e.g. the two arms of a
    Mach-Zehnder around y=0) meander apart instead of crossing each other. The
    detour length is identical either way, so length matching is unaffected."""
    dx = p2.dcenter[0] - p1.dcenter[0]
    dy = p2.dcenter[1] - p1.dcenter[1]
    if abs(dx) >= abs(dy):  # mostly horizontal: bump in y, traverse in x
        mid_y = (p1.dcenter[1] + p2.dcenter[1]) / 2
        a = amplitude_um if mid_y >= 0 else -amplitude_um
        return [{"dy": a}, {"dx": (dx / 2) or 1.0}, {"dy": -a}]
    mid_x = (p1.dcenter[0] + p2.dcenter[0]) / 2  # mostly vertical: bump in x
    a = amplitude_um if mid_x >= 0 else -amplitude_um
    return [{"dx": a}, {"dy": (dy / 2) or 1.0}, {"dx": -a}]


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


def flip_transform(transform: "Transform", axis: str) -> "Transform":
    """Reflect a placement across the screen's horizontal or vertical axis,
    about the item's own origin (position unchanged).

    axis='h' flips left↔right (x→−x); axis='v' flips top↔bottom (y→−y). The
    reflection is composed in the world frame (F · current) and re-decomposed
    by klayout into an equivalent rotation+mirror, so it stays correct for an
    already-rotated/mirrored item. Verified numerically against DCplxTrans."""
    flip = kdb.DCplxTrans(1.0, 180.0, True, 0.0, 0.0) if axis == "h" else kdb.DCplxTrans(1.0, 0.0, True, 0.0, 0.0)
    current = kdb.DCplxTrans(transform.mag, transform.rotation, transform.mirror, transform.x, transform.y)
    flipped = flip * current
    return Transform(
        x=transform.x,
        y=transform.y,
        rotation=flipped.angle,
        mirror=flipped.is_mirror(),
        mag=flipped.mag,
    )


# The project's design wavelength default (µm). Single source of truth: the
# FDTD/mode-solver wavelength defaults in fdtd_sim reference this too, so the
# simulation defaults to the same wavelength the project is designed for and the
# two can't drift apart.
DEFAULT_WAVELENGTH_UM = 1.55


@dataclass(frozen=True)
class EtchLayer:
    """A partial-etch (rib/slab) drawing layer. Geometry drawn on GDS
    (layer, datatype) is core material extruded only `slab_thickness_um` tall —
    sharing the core's bottom — instead of the full core thickness, modelling
    the slab a partial etch leaves behind (e.g. SLAB150 on layer (2, 0) of a rib
    platform). The FDTD builder and the mode solver both consume these so a rib
    waveguide is simulated as a ridge-over-slab rather than a fully-etched
    strip. `slab_thickness_um` is the *remaining* core height, not the etch
    depth (etch depth = core thickness − slab thickness)."""

    layer: int
    datatype: int = 0
    slab_thickness_um: float = 0.0


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
    # When True, the mode solver and FDTD runs ignore clad_thickness_um and
    # instead use a wavelength-scaled "effectively semi-infinite" cladding
    # extent (see fdtd_sim.effective_clad_thickness_um), so the guided mode
    # decays to nothing before reaching the domain boundary regardless of the
    # finite thickness chosen for a real process.
    clad_infinite: bool = False
    wavelength_um: float = DEFAULT_WAVELENGTH_UM
    cross_section: str = "strip"
    # Partial-etch (rib/slab) layers, drawn on top of the full-height core
    # (layer (1, 0)). Empty -> a plain strip waveguide, the original behaviour.
    etch_layers: tuple[EtchLayer, ...] = ()

    def max_slab_thickness_um(self) -> float:
        """Tallest configured slab (0.0 if none). The mode solver models a
        single idealised cross-section, so it uses the dominant slab height."""
        return max((e.slab_thickness_um for e in self.etch_layers), default=0.0)


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
        # Text notes + their callout drawings (a markup layer, never fabricated
        # — kept out of self.top like the reference backdrop). Keyed by the same
        # shared id counter as instances/routes, so ids never collide across
        # families. See model/annotation.py.
        self.annotations: dict[int, Annotation] = {}
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
        # The FDTD simulation set-up (sources, run parameters) last configured
        # for this project, persisted in the .phidler file. None until the
        # simulation window writes one, so an unconfigured project keeps the
        # window's project-settings-seeded defaults instead of stale values.
        self.simulation_config: "SimulationConfig | None" = None
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

    def _build_cell(self, component_spec: str, kwargs: dict, array: ArraySpec) -> gf.Component:
        """The effective cell for an instance: the bare component, or — when
        arrayed — a wrapper cell holding the columns×rows tiling.

        Building the array inside its own (identity) wrapper, then placing the
        wrapper as a single reference, means the instance transform rotates the
        whole array as a unit and the canvas (which renders the wrapper's
        polygons) is identical to the exported GDS (which references the same
        wrapper). The anchor element's ports are copied up so routing can still
        attach to an arrayed instance."""
        base = gf.get_component(component_spec, **kwargs)
        if not array.is_array:
            return base
        wrapper = gf.Component(f"array_{next(_array_wrapper_counter)}")
        wrapper.add_ref(
            base,
            columns=array.columns,
            rows=array.rows,
            column_pitch=array.column_pitch,
            row_pitch=array.row_pitch,
        )
        wrapper.add_ports(base.ports)
        return wrapper

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
        array: ArraySpec | None = None,
    ) -> PlacedInstance:
        kwargs = dict(kwargs or {})
        array = array or ArraySpec()
        cell = self._build_cell(component_spec, kwargs, array)
        ref = self.top.add_ref(cell)
        ref.dcplx_trans = kdb.DCplxTrans(mag, rotation, mirror, x, y)
        inst = PlacedInstance(
            id=inst_id if inst_id is not None else self.next_id(),
            component_spec=component_spec,
            kwargs=kwargs,
            cell=cell,
            ref=ref,
            array=array,
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
        """Re-insert a previously-removed instance (used by undo). inst.cell is
        already the effective (possibly arrayed) cell, so this is a plain
        single reference."""
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
        cell = self._build_cell(inst.component_spec, kwargs, inst.array)  # keeps the existing array
        inst.ref.delete()
        ref = self.top.add_ref(cell)
        ref.dcplx_trans = kdb.DCplxTrans(transform.mag, transform.rotation, transform.mirror, transform.x, transform.y)
        inst.cell = cell
        inst.ref = ref
        inst.kwargs = kwargs
        for layer_key in cell.layers:
            layer_info_for(tuple(layer_key), self.layers)
        return inst

    def set_array(self, inst_id: int, array: ArraySpec) -> PlacedInstance:
        """Re-place an instance as (or back from) an array, preserving its
        transform. Rebuilds the effective cell (the array wrapper) and its
        single reference — columns/rows are baked into the wrapper, not a
        mutable property of an existing reference."""
        inst = self.instances[inst_id]
        transform = self.get_transform(inst_id)
        cell = self._build_cell(inst.component_spec, inst.kwargs, array)
        inst.ref.delete()
        ref = self.top.add_ref(cell)
        ref.dcplx_trans = kdb.DCplxTrans(transform.mag, transform.rotation, transform.mirror, transform.x, transform.y)
        inst.cell = cell
        inst.ref = ref
        inst.array = array
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
        actually has a cut-out, e.g. for true annuli built via boolean ops.

        For an arrayed instance inst.cell is the array wrapper, so these are
        already the full columns×rows tiling — the same cell the GDS export
        references, keeping the canvas WYSIWYG."""
        return self._shapes_for_cell(self.instances[inst_id].cell)

    def _shapes_for_cell(self, cell: gf.Component) -> ShapesByLayer:
        return _shapes_from_polygons(cell.get_polygons(by="tuple"), cell.kcl.dbu, self.layers)

    def get_bbox_extent_for_instance(self, inst_id: int) -> tuple[float, float]:
        """The *base* component's (width, height) in µm — used to seed a
        sensible array pitch so the first copy doesn't land on top of the
        original. Uses the base, not inst.cell, since inst.cell may already be
        an array wrapper whose bbox is the whole grid."""
        inst = self.instances[inst_id]
        bbox = gf.get_component(inst.component_spec, **inst.kwargs).dbbox()
        return (bbox.width(), bbox.height())

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
        goal_length_um: float | None = None,
        auto_match: bool = False,
        meander_amplitude_um: float | None = None,
        diagonal: bool = False,
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

        solved_amplitude: float | None = None
        # Components the route should not cross (everything except its own two
        # endpoints), and whether the straight path between the ports runs into
        # any of them.
        obstacles = self._obstacle_bboxes({inst_a_id, inst_b_id})
        blocked = any(
            self._segment_hits_box(p1.dcenter[0], p1.dcenter[1], p2.dcenter[0], p2.dcenter[1], box)
            for box in obstacles
        )

        # A supplied amplitude (project reload or Python-script import) rebuilds
        # that exact meander deterministically; a goal without an amplitude
        # searches for one. Length matching always uses the manhattan meander,
        # so it takes priority over the diagonal flag.
        if meander_amplitude_um is not None or (auto_match and goal_length_um):
            route, solved_amplitude = self._route_to_goal_length(
                p1, p2, cross_section, goal_length_um, meander_amplitude_um
            )
            refs, length_dbu = list(route.instances), route.length
        elif diagonal and not blocked:
            # Clear straight shot — take the short diagonal path.
            refs, length_dbu = self._route_diagonal(p1, p2, cross_section)
        elif blocked:
            # A component is in the way: try a manhattan detour around it, but
            # only keep it if it genuinely clears *every* obstacle (a single
            # bump can dodge one component into another). Otherwise fall back to
            # a plain direct route — never "avoided A by crossing B".
            detour = self._route_around(p1, p2, cross_section, obstacles)
            if detour is not None and not self._route_hits_boxes(detour[0], obstacles):
                refs, length_dbu = detour
            else:
                if detour is not None:
                    for ref in detour[0]:
                        ref.delete()
                route = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section)
                refs, length_dbu = list(route.instances), route.length
        else:
            route = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section)
            refs, length_dbu = list(route.instances), route.length

        placed = PlacedRoute(
            id=route_id if route_id is not None else self.next_id(),
            instance_id_a=inst_a_id,
            port_name_a=port_a,
            instance_id_b=inst_b_id,
            port_name_b=port_b,
            cross_section=cross_section,
            refs=refs,
            length=length_dbu,
            goal_length_um=goal_length_um,
            auto_match=auto_match,
            meander_amplitude_um=solved_amplitude,
            diagonal=diagonal,
        )
        self.routes[placed.id] = placed
        for ref in placed.refs:
            for layer_key in self._shapes_for_ref(ref):
                layer_info_for(layer_key, self.layers)
        return placed

    def _route_length_um(self, route) -> float:
        return route.length * self.top.kcl.dbu

    _OBSTACLE_MARGIN_UM = 2.0  # keep routes this far clear of other components
    _DETOUR_MIN_AMP_UM = 20.0  # min perpendicular bump a default euler-bend route can realize

    def _obstacle_bboxes(self, exclude_inst_ids: set[int]) -> list:
        """Bounding boxes (in µm, with a small clearance margin) of every placed
        instance except the route's own two endpoints — the components a route
        should avoid crossing."""
        import klayout.db as kdb

        boxes = []
        m = self._OBSTACLE_MARGIN_UM
        for inst_id, placed in self.instances.items():
            if inst_id in exclude_inst_ids:
                continue
            b = placed.ref.dbbox()
            boxes.append(kdb.DBox(b.left - m, b.bottom - m, b.right + m, b.top + m))
        return boxes

    @staticmethod
    def _segment_hits_box(x1: float, y1: float, x2: float, y2: float, box) -> bool:
        """Liang–Barsky: does the segment (x1,y1)-(x2,y2) cross the box? Used as
        a cheap proxy for 'would a direct route pass through this component'."""
        dx, dy = x2 - x1, y2 - y1
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x1 - box.left), (dx, box.right - x1), (-dy, y1 - box.bottom), (dy, box.top - y1)):
            if p == 0:
                if q < 0:
                    return False
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        return False
                    t0 = max(t0, t)
                else:
                    if t < t0:
                        return False
                    t1 = min(t1, t)
        return t0 <= t1

    def _route_hits_boxes(self, refs, boxes: list) -> bool:
        """Does the built route's actual geometry intersect any of the boxes?
        The verify-after guard for the detour heuristic: a single bump can
        dodge one component and run straight through another, so the finished
        route is checked against every obstacle and thrown away if it still
        hits one."""
        import klayout.db as kdb

        dbu = self.top.kcl.dbu
        region = kdb.Region()
        for ref in refs:
            for shapes in self._shapes_for_ref(ref).values():
                for hull, _holes in shapes:
                    region.insert(kdb.Polygon([kdb.Point(round(x / dbu), round(y / dbu)) for x, y in hull]))
        for b in boxes:
            ibox = kdb.Box(round(b.left / dbu), round(b.bottom / dbu), round(b.right / dbu), round(b.top / dbu))
            if not (region & kdb.Region(ibox)).is_empty():
                return True
        return False

    def _route_around(self, p1, p2, cross_section: str, obstacles: list):
        """Best-effort manhattan detour around the components blocking the
        straight path: a single perpendicular bump sized to clear them, then
        traverse, then bump back. Returns (refs, length_in_dbu) or None if no
        bump can be realized. This is a heuristic, not a router — the caller
        verifies the result with _route_hits_boxes and discards it if it still
        crosses something."""
        x1, y1 = p1.dcenter[0], p1.dcenter[1]
        x2, y2 = p2.dcenter[0], p2.dcenter[1]
        blockers = [b for b in obstacles if self._segment_hits_box(x1, y1, x2, y2, b)]
        if not blockers:
            return None
        margin = self._OBSTACLE_MARGIN_UM
        floor = self._DETOUR_MIN_AMP_UM  # euler bends need room; smaller bumps don't realize
        # The bump must span the obstacles' extent along the route axis (advance
        # up to just before them, go around, come back just after) — a bump that
        # rises and falls in the wrong place clears nothing.
        if abs(x2 - x1) >= abs(y2 - y1):  # horizontal route: detour in y
            line_y = (y1 + y2) / 2
            up = max(b.top for b in blockers) - line_y + margin
            down = line_y - min(b.bottom for b in blockers) + margin
            amp = max(up, floor) if up <= down else -max(down, floor)
            left = min(b.left for b in blockers) - floor
            right = max(b.right for b in blockers) + floor
            x_near, x_far = (left, right) if x2 >= x1 else (right, left)
            steps = [{"x": x_near, "y": line_y}, {"x": x_near, "y": line_y + amp},
                     {"x": x_far, "y": line_y + amp}, {"x": x_far, "y": line_y}]
        else:  # vertical route: detour in x
            line_x = (x1 + x2) / 2
            right = max(b.right for b in blockers) - line_x + margin
            left = line_x - min(b.left for b in blockers) + margin
            amp = max(right, floor) if right <= left else -max(left, floor)
            bottom = min(b.bottom for b in blockers) - floor
            top = max(b.top for b in blockers) + floor
            y_near, y_far = (bottom, top) if y2 >= y1 else (top, bottom)
            steps = [{"x": line_x, "y": y_near}, {"x": line_x + amp, "y": y_near},
                     {"x": line_x + amp, "y": y_far}, {"x": line_x, "y": y_far}]
        try:
            route = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section, steps=steps)
            if not route.instances:
                return None
            return list(route.instances), route.length
        except Exception:
            return None

    def _route_diagonal(self, p1, p2, cross_section: str):
        """Route p1→p2 directly with all-angle (diagonal) euler bends, so it
        takes the short diagonal path instead of a manhattan L/U-turn. Returns
        (refs, length_in_dbu).

        gdsfactory's all-angle router produces virtual, off-grid instances
        (VInstance / ComponentAllAngle) that this app can neither render
        per-ref nor delete. So the route is built in a throwaway holder,
        flattened to plain polygons, and added back to the top cell as a single
        ordinary reference — which renders, deletes, and exports through the
        normal path like any other route. Falls back to the manhattan
        route_single when gdsfactory can't realize the all-angle route (ports
        too close for the bend radius, or an orientation it can't resolve).

        route_bundle_all_angle reports length in µm; the rest of this module
        works in database units, so the diagonal length is normalised to dbu."""
        try:
            holder = gf.Component()
            routes = gf.routing.route_bundle_all_angle(holder, [p1], [p2], cross_section=cross_section)
            route = routes[0]
            if not route.instances:
                raise ValueError("all-angle route is empty")
            length_dbu = route.length / self.top.kcl.dbu
            for vinst in route.instances:
                vinst.insert_into(holder)
            holder.flatten()
            ref = self.top.add_ref(holder)
            return [ref], length_dbu
        except Exception:
            route = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section)
            return list(route.instances), route.length

    def _route_with_meander(self, p1, p2, cross_section: str, amplitude_um: float):
        """Route p1→p2 with a single perpendicular detour ('bump') of the given
        amplitude. route_single defaults to euler bends, so the detour is an
        adiabatic curve (low-loss), per the length-matching requirement.
        Returns the route, or None if route_single can't realize this bump for
        the ports' geometry (it returns a degenerate ~0-length route, which we
        treat as failure)."""
        steps = _meander_steps(p1, p2, amplitude_um)
        try:
            route = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section, steps=steps)
        except Exception:
            return None
        return route

    def meander_steps_for_route(self, route: PlacedRoute) -> list[dict] | None:
        """The route_single `steps` that reproduce an auto-matched route's
        meander, or None for a route with no meander. Lets the Python-script
        exporter emit the same detour instead of a (shorter) natural route."""
        if route.meander_amplitude_um is None:
            return None
        p1 = self.instances[route.instance_id_a].ref.ports[route.port_name_a]
        p2 = self.instances[route.instance_id_b].ref.ports[route.port_name_b]
        return _meander_steps(p1, p2, route.meander_amplitude_um)

    def _route_to_goal_length(self, p1, p2, cross_section: str, goal_um: float, amplitude_um: float | None):
        """Best-effort adiabatic length matching. Returns (route, amplitude).

        Manual mode is the implicit fallback: if no valid meander can reach the
        goal (degenerate route for this geometry, or goal shorter than the
        natural route), the natural route is used and amplitude is None — the
        UI then just reports actual-vs-goal, which is the manual workflow."""
        natural = gf.routing.route_single(self.top, p1, p2, cross_section=cross_section)
        natural_len = self._route_length_um(natural)

        # Replay path (project/script reload): an amplitude was already solved,
        # so rebuild that exact geometry deterministically without searching.
        if amplitude_um is not None:
            for ref in natural.instances:
                ref.delete()
            route = self._route_with_meander(p1, p2, cross_section, amplitude_um)
            if route is not None and self._route_length_um(route) >= natural_len * 0.99:
                return route, amplitude_um
            # Stored amplitude no longer valid (geometry changed): fall back.
            return gf.routing.route_single(self.top, p1, p2, cross_section=cross_section), None

        if goal_um <= natural_len:
            return natural, None  # can't shorten below the natural route

        for ref in natural.instances:
            ref.delete()
        amplitude = self._search_meander_amplitude(p1, p2, cross_section, goal_um, natural_len)
        if amplitude is None:
            return gf.routing.route_single(self.top, p1, p2, cross_section=cross_section), None
        route = self._route_with_meander(p1, p2, cross_section, amplitude)
        if route is None:
            return gf.routing.route_single(self.top, p1, p2, cross_section=cross_section), None
        return route, amplitude

    def _search_meander_amplitude(self, p1, p2, cross_section, goal_um, natural_len):
        """Binary-search the detour amplitude whose route length ≈ goal. Only
        amplitudes that yield a *valid* route (length ≥ natural) count as
        samples — route_single silently returns ~0 length for an unrealizable
        bump, which must never be treated as a real measurement. Returns the
        best amplitude, or None if even the largest bump can't be realized."""
        lo, hi = _MEANDER_MIN_AMPLITUDE_UM, _MEANDER_MAX_AMPLITUDE_UM

        def sample(a: float):
            route = self._route_with_meander(p1, p2, cross_section, a)
            if route is None:
                return None
            length = self._route_length_um(route)
            for ref in route.instances:
                ref.delete()
            return length if length >= natural_len * 0.99 else None

        if sample(hi) is None:  # geometry can't take a meander at all -> manual fallback
            return None
        best = hi
        for _ in range(28):
            mid = (lo + hi) / 2
            length = sample(mid)
            if length is None:  # amplitude too small to realize: search higher
                lo = mid
                continue
            best = mid
            if abs(length - goal_um) <= _MEANDER_TOLERANCE_UM:
                return mid
            if length < goal_um:
                lo = mid
            else:
                hi = mid
        return best

    def remove_route(self, route_id: int) -> PlacedRoute:
        route = self.routes.pop(route_id)
        for ref in route.refs:
            ref.delete()
        return route

    def routes_for_instance(self, inst_id: int) -> list[int]:
        """Ids of every route with this instance as an endpoint — the routes
        that need re-routing when the instance moves."""
        return [
            rid
            for rid, r in self.routes.items()
            if r.instance_id_a == inst_id or r.instance_id_b == inst_id
        ]

    def rebuild_route(self, route_id: int):
        """Re-route an existing route between its endpoints' *current* port
        positions, keeping its id and recipe (cross-section, length goal,
        diagonal, obstacle avoidance) — used after a component moves so the
        track follows. Re-runs the full routing decision (a moved component may
        now block, or stop blocking, the path; a length goal re-searches its
        meander for the new separation). Drops the route if an endpoint
        instance no longer exists. Returns the rebuilt PlacedRoute or None."""
        old = self.routes.get(route_id)
        if old is None:
            return None
        if old.instance_id_a not in self.instances or old.instance_id_b not in self.instances:
            self.remove_route(route_id)
            return None
        for ref in old.refs:
            ref.delete()
        return self.add_route(
            old.instance_id_a,
            old.port_name_a,
            old.instance_id_b,
            old.port_name_b,
            old.cross_section,
            route_id=route_id,
            goal_length_um=old.goal_length_um,
            auto_match=old.auto_match,
            diagonal=old.diagonal,
        )

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

    # -- annotations (notes + callout drawings) -----------------------------

    def add_annotation(
        self,
        text: str,
        x: float,
        y: float,
        shapes: list[CalloutShape] | None = None,
        color: str = DEFAULT_ANNOTATION_COLOR,
        ann_id: int | None = None,
    ) -> Annotation:
        """Create a note pinned at ``(x, y)`` (µm). ``ann_id`` is supplied on
        load/redo so a re-created note keeps its id (mirrors add_instance/
        add_route); otherwise a fresh id is drawn from the shared counter."""
        ann = Annotation(
            id=ann_id if ann_id is not None else self.next_id(),
            text=text,
            x=x,
            y=y,
            shapes=list(shapes) if shapes else [],
            color=color,
        )
        self.annotations[ann.id] = ann
        return ann

    def remove_annotation(self, ann_id: int) -> Annotation:
        return self.annotations.pop(ann_id)

    def restore_annotation(self, annotation: Annotation) -> None:
        """Re-insert a previously-removed note (used by delete-undo), keeping
        its object identity — and thus its shapes and id — intact."""
        self.annotations[annotation.id] = annotation

    def set_annotation_position(self, ann_id: int, x: float, y: float) -> None:
        ann = self.annotations[ann_id]
        ann.x, ann.y = x, y

    def set_annotation_text(self, ann_id: int, text: str) -> None:
        self.annotations[ann_id].text = text

    def add_annotation_shape(self, ann_id: int, shape: CalloutShape) -> None:
        self.annotations[ann_id].shapes.append(shape)

    def remove_annotation_shape(self, ann_id: int, shape: CalloutShape) -> None:
        self.annotations[ann_id].shapes.remove(shape)

    # -- whole-document reset (File > New, or before loading a project) -----

    def clear_all(self) -> tuple[list[int], list[int]]:
        """Removes every instance and route. Returns (removed_instance_ids,
        removed_route_ids) so the caller (which also owns the Qt scene
        items) can remove the matching graphics items. Annotations are cleared
        too; the scene drops their items via clear_annotation_items() since they
        aren't part of the returned tuple."""
        removed_instance_ids = list(self.instances)
        removed_route_ids = list(self.routes)
        for route_id in removed_route_ids:
            self.remove_route(route_id)
        for inst_id in removed_instance_ids:
            self.remove_instance(inst_id)
        self.annotations.clear()
        self.clear_reference()
        self.custom_component_paths = []
        self.simulation_config = None
        return removed_instance_ids, removed_route_ids

    # -- export --------------------------------------------------------------

    def export_gds(self, path: str) -> str:
        return str(self.top.write_gds(path))
