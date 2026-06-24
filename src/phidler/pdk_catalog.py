from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

import gdsfactory as gf

EDITABLE_PARAM_TYPES = (bool, int, float, str)  # order matters: bool before int (bool is an int subclass)

# Categories considered core to building a photonic circuit (vs. the generic
# PDK's other domains: MEMS, quantum/superconducting electronics,
# microfluidics, analog RF, process-control-monitor test structures, etc.).
# Order here is the display order in the palette.
CORE_CATEGORIES = [
    "waveguides",
    "bends",
    "tapers",
    "couplers",
    "edge_couplers",
    "mmis",
    "rings",
    "mzis",
    "grating_couplers",
    "filters",
    "spirals",
    "detectors",
]

CUSTOM_CATEGORY = "custom"

_CATEGORY_DISPLAY_NAMES = {
    "waveguides": "Waveguides",
    "bends": "Bends",
    "tapers": "Tapers",
    "couplers": "Couplers",
    "edge_couplers": "Edge Couplers",
    "mmis": "MMIs",
    "rings": "Rings",
    "mzis": "MZIs",
    "grating_couplers": "Grating Couplers",
    "filters": "Filters",
    "spirals": "Spirals",
    "detectors": "Detectors",
    "analog": "Analog",
    "containers": "Containers",
    "dies": "Dies",
    "mems": "MEMS",
    "microfluidics": "Microfluidics",
    "pads": "Pads",
    "pcms": "PCMs",
    "quantum": "Quantum",
    "shapes": "Shapes",
    "superconductors": "Superconductors",
    "texts": "Texts",
    "vias": "Vias",
    "other": "Other",
    CUSTOM_CATEGORY: "Custom",
}

_ACRONYMS = {
    "mmi", "mzi", "dbr", "awg", "cdsem", "te", "tm", "rf", "dc", "ge", "si",
    "gsg", "gs", "pn", "npp", "ppp", "pp", "np", "io", "gds", "cpw", "snspd",
}  # fmt: skip

_ALPHA_DIGIT_RE = re.compile(r"^([a-zA-Z]*)(\d.*)?$")


def category_display_name(category: str) -> str:
    return _CATEGORY_DISPLAY_NAMES.get(category, category.replace("_", " ").title())


def prettify_component_name(name: str) -> str:
    """"mmi1x2" -> "MMI 1x2", "via_stack_corner45_extended" -> "Via Stack
    Corner 45 Extended". A heuristic, not a lookup table — verified against
    a random sample of the real catalog rather than a few hand-picked
    examples, but won't be perfect for every name (e.g. "mzit_lattice"
    stays "Mzit Lattice" since "mzit" isn't recognized as "mzi" + "t").
    The raw name is always kept as the underlying component_spec and shown
    as a tooltip, so an imperfect prettification never affects placement
    or save/load — only the label."""
    pretty_words = []
    for raw in name.split("_"):
        match = _ALPHA_DIGIT_RE.match(raw)
        alpha, digits = match.group(1), match.group(2)
        if alpha and digits and len(alpha) <= 2:
            # a short prefix + digits reads better combined: "m1" -> "M1",
            # not "M 1" (this is the common metal/via-layer naming style)
            pretty_words.append((alpha + digits).upper())
            continue
        if alpha:
            pretty_words.append(alpha.upper() if alpha.lower() in _ACRONYMS else alpha.capitalize())
        if digits:
            pretty_words.append(digits)
    return " ".join(pretty_words)


@dataclass
class ComponentSpec:
    name: str
    category: str
    factory: Callable[..., Any]
    signature: inspect.Signature


def _category_for(factory: Callable) -> str:
    mod = getattr(factory, "__module__", "") or ""
    parts = mod.split(".")
    if len(parts) > 2 and parts[0] == "gdsfactory" and parts[1] == "components":
        return parts[2]
    return "other"


def build_catalog() -> dict[str, list[ComponentSpec]]:
    """The placeable subset of gf.components: callables (not submodules or
    classes) that take no required arguments, AND are actually registered
    in the active PDK's cell registry.

    That last check matters: gf.components contains things that pass every
    other filter (zero-arg-callable) but aren't real standalone placeable
    cells — e.g. a class like SequenceGenerator, or wrapper/measurement
    utilities like add_termination/array/cutback_bend90 that gf.get_component
    rejects by name with "not in PDK". Caught by a 120-component placement
    test, not by hand-picking a handful of obviously-real components."""
    active_pdk = gf.get_active_pdk()
    registered_names = set(active_pdk.cells)

    catalog: dict[str, list[ComponentSpec]] = {}
    for name in dir(gf.components):
        if name.startswith("_") or name not in registered_names:
            continue
        factory = getattr(gf.components, name)
        if not callable(factory) or inspect.isclass(factory) or inspect.ismodule(factory):
            continue
        try:
            sig = inspect.signature(factory)
        except (TypeError, ValueError):
            continue
        required = [
            p.name
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if required:
            continue
        if "ComponentAllAngle" in str(sig.return_annotation):
            # a different placement primitive (Component.add_ref_off_grid(),
            # not the regular add_ref() this app's document model uses
            # throughout) — caught by the same 120-component placement test.
            continue
        category = _category_for(factory)
        catalog.setdefault(category, []).append(
            ComponentSpec(name=name, category=category, factory=factory, signature=sig)
        )
    for items in catalog.values():
        items.sort(key=lambda c: c.name)
    return catalog


def editable_defaults(spec: ComponentSpec) -> dict[str, Any]:
    """Scalar-typed (bool/int/float/str) parameter defaults, for seeding the
    properties panel. Non-scalar defaults (ComponentSpec/CrossSectionSpec
    callables etc.) are shown read-only by the properties panel instead."""
    return {
        p.name: p.default
        for p in spec.signature.parameters.values()
        if p.default is not inspect.Parameter.empty and isinstance(p.default, EDITABLE_PARAM_TYPES)
    }


def list_cross_section_names() -> list[str]:
    """Valid cross_section names for the active PDK — used to constrain
    both the route cross-section picker and the properties panel's
    cross_section field to values that will actually work, rather than
    accepting freeform text that get_component/route_single would reject."""
    return sorted(gf.get_active_pdk().cross_sections.keys())
