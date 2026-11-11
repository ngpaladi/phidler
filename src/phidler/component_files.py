"""Component files (``*.pcomp.json``): photonic parts as one shareable file,
like a KiCad library part (see photonfdtd.circuit.library).

A component file carries a part's fixed layout, its ports, its electrical
pins and its circuit model (solved physics, S-parameter tables and the recipe
they came from), and optionally a generator to rebuild it with other
parameters. Phidler

- imports them (File > Import Component…) into the palette, where they place
  like built-ins and simulate in the Circuit tab with their own models;
- scans library folders (File > Component Libraries…) at start-up, one
  palette category per folder;
- exports the selection as one (File > Export Selection as Component…): a
  single instance keeps its kind and generator, several become a circuit of
  their parts with the open ports and pads as the part's pins.

This module is the GUI-free part.
"""
from __future__ import annotations

import inspect
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from .pdk_catalog import ComponentSpec

try:
    from photonfdtd.circuit import library as pclib

    SUFFIX = pclib.SUFFIX
    AVAILABLE = True
except Exception:  # pragma: no cover - older photonfdtd
    pclib = None
    SUFFIX = ".pcomp.json"
    AVAILABLE = False

#: Palette category of imported component files.
CATEGORY = "component_files"
SETTINGS_KEY = "component_library_dirs"
#: Content keys of component files whose generator code the user trusted.
TRUST_KEY = "trusted_component_files"


def _settings(settings=None):
    from PySide6.QtCore import QSettings

    return settings if settings is not None else QSettings("phidler", "phidler")


def _trusted_keys(settings=None) -> list[str]:
    stored = _settings(settings).value(TRUST_KEY, [])
    if isinstance(stored, str):  # QSettings collapses a 1-element list to a bare string
        stored = [stored]
    return [str(k) for k in (stored or [])]


def is_trusted(key: str, settings=None) -> bool:
    """Whether the user trusted this exact file content before."""
    return key in _trusted_keys(settings)


def remember_trust(key: str, settings=None) -> None:
    """Trust this file content from now on (a changed file asks again)."""
    keys = _trusted_keys(settings)
    if key not in keys:
        _settings(settings).setValue(TRUST_KEY, keys + [key])


def file_key(path: str) -> str:
    """The content key of a component file (changes with any edit)."""
    return pclib.load_component(path).key


def is_component_file(path: str) -> bool:
    return str(path).endswith(SUFFIX)


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_") or "part"


def generator_usable(cf) -> bool:
    g = cf.data.get("generator")
    if not g:
        return False
    if "source" in g:
        return cf.trust
    return cf.trust or pclib.generator_allowed(g)


def needs_trust(path: str) -> bool:
    """Whether the file carries generator source code (which runs only if
    the user trusts it)."""
    import json

    data = json.loads(Path(path).read_text())
    return "source" in (data.get("generator") or {})


@dataclass
class LoadedFile:
    spec: ComponentSpec
    component_file: object


def load_component_file(path: str, trust: bool = False, category: str = CATEGORY,
                        settings=None) -> LoadedFile:
    """Load ``path`` and register it with the active PDK as a placeable cell
    named ``pcomp_<name>``. With a usable generator the cell takes the
    generator's parameters (left at their defaults it is the file's fixed
    geometry); otherwise it takes none. A file whose content the user
    trusted before (:func:`remember_trust`) is trusted again."""
    if not AVAILABLE:
        raise RuntimeError("component files need photonfdtd with photonfdtd.circuit (0.16+)")
    import gdsfactory as gf

    cf = pclib.load_component(path, trust=trust)
    if not cf.trust and is_trusted(cf.key, settings):
        cf = pclib.load_component(path, trust=True)
    name = f"pcomp_{_safe(cf.name)}"
    pdk = gf.get_active_pdk()
    existing = pdk.cells.get(name)
    if existing is not None and getattr(existing, "_pcomp_key", None) not in (None, cf.key):
        name = f"{name}_{cf.key[:6]}"
    defaults = dict(cf.data.get("generator", {}).get("kwargs", {})) if generator_usable(cf) else {}

    def factory(**kwargs):
        changed = {k: v for k, v in kwargs.items() if defaults.get(k) != v}
        if changed and not generator_usable(cf):
            # E.g. a project saved while the file was trusted: keep the
            # instance, with the file's fixed geometry.
            warnings.warn(f"component file {cf.name!r}: its generator is not trusted, so "
                          f"{sorted(changed)} are ignored and the fixed geometry is used",
                          stacklevel=2)
            return cf.cell()
        if changed:
            return cf.regenerate(**{**defaults, **kwargs})
        return cf.cell()

    factory.__name__ = name
    factory.__module__ = __name__
    factory.__doc__ = cf.data.get("description") or f"Component file {cf.name}"
    factory.__signature__ = inspect.Signature([
        inspect.Parameter(k, inspect.Parameter.KEYWORD_ONLY, default=v)
        for k, v in defaults.items() if isinstance(v, (bool, int, float, str))
    ])
    factory._pcomp_key = cf.key
    pdk.register_cells(**{name: factory})
    spec = ComponentSpec(name=name, category=category, factory=factory,
                         signature=factory.__signature__)
    return LoadedFile(spec, cf)


def scan_library(folder: str) -> tuple[list[LoadedFile], list[str]]:
    """Load every component file in ``folder`` (untrusted: generators carrying
    source stay off). Returns the loaded files and the ones that failed."""
    loaded, failed = [], []
    root = Path(folder)
    category = f"library:{root.name}"
    for path in sorted(root.glob(f"*{SUFFIX}")):
        try:
            loaded.append(load_component_file(str(path), category=category))
        except Exception as exc:
            failed.append(f"{path.name}: {exc}")
    return loaded, failed


# --------------------------------------------------------------------------- #
# Export.
# --------------------------------------------------------------------------- #
def selection_component(document, inst_ids: list[int]):
    """A gdsfactory component of the selected instances and the routes
    from them, with every open port exposed: optical ports as ``o1``,
    ``o2``, … (left to right), electrical ports as ``<spec><id>_<port>``,
    and the ends of routes to unselected parts (``to_<spec><id>_<port>``
    when electrical)."""
    import gdsfactory as gf

    ids = set(inst_ids)
    c = gf.Component()
    refs = {}
    for i in inst_ids:
        inst = document.instances[i]
        ref = c.add_ref(inst.cell)
        ref.dcplx_trans = inst.ref.dcplx_trans
        refs[i] = ref
    all_ports = [(i, p) for i, ref in refs.items() for p in ref.ports]

    def pkey(p):
        return (round(p.center[0], 3), round(p.center[1], 3))
    # Routing pieces (segments, bends, the tapers routing adds at the ends)
    # are plain instances of the top cell. Group them into routes (pieces
    # joined port to port, but not through an instance's port) and take
    # every route that reaches the selection. Where one ends on a part left
    # out, its end becomes a pin of the export.
    placed_names = {inst.ref.name for inst in document.instances.values()}
    pieces = [r for r in document.top.insts if r.name not in placed_names]
    selected = {pkey(p) for _, p in all_ports}
    others = {pkey(p): (i, p.name) for i, inst in document.instances.items() if i not in ids
              for p in inst.ref.ports}
    parent = list(range(len(pieces)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    owner: dict = {}
    for n, r in enumerate(pieces):
        for p in r.ports:
            k = pkey(p)
            if k in selected or k in others:
                continue
            if k in owner:
                parent[find(n)] = find(owner[k])
            else:
                owner[k] = n
    routes: dict = {}
    for n, r in enumerate(pieces):
        routes.setdefault(find(n), []).append(r)
    for route in routes.values():
        if not {pkey(p) for r in route for p in r.ports} & selected:
            continue
        for r in route:
            nr = c.add_ref(r.cell)
            nr.dcplx_trans = r.dcplx_trans
            all_ports += [(None, p) for p in nr.ports]

    count: dict = {}
    for _, p in all_ports:
        count[pkey(p)] = count.get(pkey(p), 0) + 1
    open_optical, open_elec, seen = [], [], set()
    for i, p in all_ports:
        k = pkey(p)
        if count[k] > 1 and not (i is None and k in others):
            continue
        if i is None:
            if k not in others or k in seen:
                continue      # a route's inner joint, or an end already taken
            seen.add(k)
        (open_optical if p.port_type == "optical" else open_elec).append((i, p))
    open_optical.sort(key=lambda ip: (round(ip[1].center[0], 1), ip[1].center[1]))
    for n, (i, p) in enumerate(open_optical, 1):
        c.add_port(f"o{n}", port=p)
    for i, p in open_elec:
        if i is None:     # a route's end on a part left out
            j, name = others[pkey(p)]
            c.add_port(f"to_{_safe(document.instances[j].component_spec)}{j}_{name}", port=p)
        else:
            spec = _safe(document.instances[i].component_spec)
            c.add_port(f"{spec}{i}_{p.name}", port=p)
    return c


def export_selection(document, inst_ids: list[int], path: str, *, name: str,
                     description: str = "", author: str = "", library: str = "",
                     sparams_wavelengths=None, waveguide=None) -> dict:
    """Write the selected instances as a component file. One instance keeps
    its own kind, physics and generator; several become a ``circuit``. A
    selection photonfdtd has no model for is written without one, with a
    warning.
    ``sparams_wavelengths`` (um) adds an S-parameter table sampled from the
    model at 0 V."""
    if not AVAILABLE:
        raise RuntimeError("component files need photonfdtd with photonfdtd.circuit (0.16+)")
    if not inst_ids:
        raise ValueError("nothing selected")
    # One instance keeps its own kind (an imported part keeps its file's
    # model, pins and generator); several become a circuit.
    comp = (document.instances[inst_ids[0]].cell if len(inst_ids) == 1
            else selection_component(document, inst_ids))
    meta = dict(name=name, description=description, author=author, library=library)
    try:
        data = pclib.export_component(comp, None, waveguide=waveguide, **meta)
    except ValueError as exc:
        # Nothing photonfdtd can model (a ring, a grating coupler, ...):
        # export the layout, ports and generator, with no model.
        warnings.warn(f"exported without a circuit model: {exc}", stacklevel=2)
        data = pclib.export_component(comp, None, model=None, **meta)
    if sparams_wavelengths is not None and data["model"].get("physics"):
        m = pclib._model_from_physics(data["model"]["physics"])
        data["model"]["sparams"] = pclib.sample_sparams(m, sparams_wavelengths)
    p = Path(path)
    if not p.name.endswith(SUFFIX):
        p = p.with_name(p.name + SUFFIX)
    import json

    p.write_text(json.dumps(data, indent=1))
    return data
