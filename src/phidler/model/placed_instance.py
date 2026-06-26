from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArraySpec:
    """How a single placed component is tiled into a rectangular array.

    The default (1×1, zero pitch) is a plain single placement — `is_array`
    is False and nothing downstream treats it specially. columns runs along
    +x, rows along +y, both in the component's own (pre-transform) frame, so
    the array tiles before the instance's rotation/mirror/scale is applied
    (matching gdsfactory's array-reference semantics)."""

    columns: int = 1
    rows: int = 1
    column_pitch: float = 0.0  # µm between columns (x)
    row_pitch: float = 0.0  # µm between rows (y)

    @property
    def is_array(self) -> bool:
        return self.columns > 1 or self.rows > 1


@dataclass
class PlacedInstance:
    """A single placed component: the gdsfactory cell/ref pair plus the spec
    needed to regenerate it (e.g. after a property-panel edit).

    cell/ref are excluded from the default repr (repr=False): their own
    reprs recursively dump the entire underlying KCell, which is harmless
    in code but unreadable noise the moment this object is returned from
    the scripting console — place(...) without assigning the result would
    otherwise auto-echo a multi-thousand-character wall of text (confirmed
    while capturing a console screenshot for the README)."""

    id: int
    component_spec: str
    kwargs: dict[str, Any]
    cell: Any = field(repr=False)  # gf.Component for this instance's geometry (unplaced/local frame)
    ref: Any = field(repr=False)  # kfactory DInstance placed into the document's top cell
    array: ArraySpec = field(default_factory=ArraySpec)
    label: str = ""
    locked: bool = False


@dataclass
class PlacedRoute:
    """A route created by gf.routing.route_single between two ports. Tracks
    every ref it created so the whole route can be deleted/undone as a unit.

    refs is excluded from the default repr for the same reason as
    PlacedInstance.cell/ref above — see that docstring."""

    id: int
    instance_id_a: int
    port_name_a: str
    instance_id_b: int
    port_name_b: str
    cross_section: str
    refs: list[Any] = field(default_factory=list, repr=False)
    length: float = 0.0
    # Optional length goal. goal_length_um is what the user asked for (in µm,
    # converted from time at the input if needed). auto_match=True means the
    # router inserted an adiabatic meander to approach it; meander_amplitude_um
    # is the solved bump size, persisted so a project reload rebuilds the same
    # geometry deterministically instead of re-searching.
    goal_length_um: float | None = None
    auto_match: bool = False
    meander_amplitude_um: float | None = None
    # Route directly with all-angle (diagonal) euler bends instead of the
    # manhattan default, so a route can take the short diagonal path rather than
    # U-turning on port orientation. Ignored when a length goal is set (those
    # use the manhattan meander).
    diagonal: bool = False
