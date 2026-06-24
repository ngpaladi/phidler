from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
