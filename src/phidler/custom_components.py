from __future__ import annotations

import importlib.util
import inspect
import sys
import uuid
from dataclasses import dataclass

import gdsfactory as gf

from .pdk_catalog import CUSTOM_CATEGORY, ComponentSpec


@dataclass
class CustomComponentsResult:
    specs: dict[str, ComponentSpec]
    skipped: list[str]


def load_custom_components(path: str) -> CustomComponentsResult:
    """Loads a user's Python file and finds every component factory it
    defines (not merely imports — re-exported names like `from
    gdsfactory.components import straight` are excluded via __module__,
    confirmed empirically that gf.cell-decorated functions correctly
    preserve __module__ pointing at the defining file, not gdsfactory's).

    A "component factory" here means: a callable taking no required
    arguments (so it can be placed immediately, same constraint as the
    built-in catalog) that actually returns a gf.Component when called —
    verified by calling it once, not just inspected, since this is
    user-supplied and unvetted code, unlike the exhaustively-tested
    built-in catalog.

    Each valid factory is registered with the active PDK under its
    function name via register_cells(), so it can be resolved later by
    name through gf.get_component() — which is how this app's document
    model (and save/load, which stores component_spec as a plain string)
    always looks components up, never by holding a direct reference to the
    factory itself.
    """
    module_name = f"phidler_custom_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load a Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(f"Error executing {path}: {exc}") from exc

    pdk = gf.get_active_pdk()
    found: dict[str, ComponentSpec] = {}
    skipped: list[str] = []

    for name, factory in vars(module).items():
        if name.startswith("_"):
            continue
        if not callable(factory) or inspect.isclass(factory) or inspect.ismodule(factory):
            continue
        if getattr(factory, "__module__", None) != module_name:
            continue  # imported into the file, not defined in it
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
            skipped.append(name)
            continue
        try:
            result = factory()
        except Exception:
            skipped.append(name)
            continue
        if not isinstance(result, gf.Component):
            skipped.append(name)
            continue

        pdk.register_cells(**{name: factory})
        found[name] = ComponentSpec(name=name, category=CUSTOM_CATEGORY, factory=factory, signature=sig)

    return CustomComponentsResult(specs=found, skipped=skipped)
