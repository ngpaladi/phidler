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
    return (g.get("module", "") + ".").startswith(pclib.TRUSTED_MODULES) or cf.trust


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


def load_component_file(path: str, trust: bool = False, category: str = CATEGORY) -> LoadedFile:
    """Load ``path`` and register it with the active PDK as a placeable cell
    named ``pcomp_<name>``. With a usable generator the cell takes the
    generator's parameters (left at their defaults it is the file's fixed
    geometry); otherwise it takes none."""
    if not AVAILABLE:
        raise RuntimeError("component files need photonfdtd with photonfdtd.circuit (0.16+)")
    import gdsfactory as gf

    cf = pclib.load_component(path, trust=trust)
    name = f"pcomp_{_safe(cf.name)}"
    pdk = gf.get_active_pdk()
    existing = pdk.cells.get(name)
    if existing is not None and getattr(existing, "_pcomp_key", None) not in (None, cf.key):
        name = f"{name}_{cf.key[:6]}"
    defaults = dict(cf.data.get("generator", {}).get("kwargs", {})) if generator_usable(cf) else {}

    def factory(**kwargs):
        changed = {k: v for k, v in kwargs.items() if defaults.get(k) != v}
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
    between them, with every open port exposed: optical ports as ``o1``,
    ``o2``, … (left to right), electrical ports as ``<pad>_<port>``."""
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
    # are plain instances of the top cell: take every one that chains, port
    # to port, from the selection.
    placed_names = {inst.ref.name for inst in document.instances.values()}
    pieces = [r for r in document.top.insts if r.name not in placed_names]
    reached = {pkey(p) for _, p in all_ports}
    taken: set = set()
    grew = True
    while grew:
        grew = False
        for r in pieces:
            if r.name in taken:
                continue
            if any(pkey(p) in reached for p in r.ports):
                taken.add(r.name)
                grew = True
                nr = c.add_ref(r.cell)
                nr.dcplx_trans = r.dcplx_trans
                for p in r.ports:
                    reached.add(pkey(p))
                all_ports += [(None, p) for p in nr.ports]

    key = pkey
    count = {}
    for _, p in all_ports:
        count[key(p)] = count.get(key(p), 0) + 1
    open_optical, open_elec = [], []
    for i, p in all_ports:
        if i is None or count[key(p)] > 1:
            continue
        (open_optical if p.port_type == "optical" else open_elec).append((i, p))
    open_optical.sort(key=lambda ip: (round(ip[1].center[0], 1), ip[1].center[1]))
    for n, (i, p) in enumerate(open_optical, 1):
        c.add_port(f"o{n}", port=p)
    for i, p in open_elec:
        spec = _safe(document.instances[i].component_spec)
        c.add_port(f"{spec}{i}_{p.name}", port=p)
    return c


def export_selection(document, inst_ids: list[int], path: str, *, name: str,
                     description: str = "", author: str = "", library: str = "",
                     sparams_wavelengths=None, waveguide=None) -> dict:
    """Write the selected instances as a component file. One instance keeps
    its own kind, physics and generator; several become a ``circuit``.
    ``sparams_wavelengths`` (um) adds an S-parameter table sampled from the
    model at 0 V."""
    if not AVAILABLE:
        raise RuntimeError("component files need photonfdtd with photonfdtd.circuit (0.16+)")
    if not inst_ids:
        raise ValueError("nothing selected")
    if len(inst_ids) == 1:
        inst = document.instances[inst_ids[0]]
        comp = inst.cell
        info = dict(comp.info) if comp.info else {}
        if "pcomp" in info:          # re-exporting an imported part: keep its file's model
            data = pclib._registry_get(info["pcomp"]).data
            model = data.get("model", {}).get("physics")
        else:
            model = "auto"
    else:
        comp = selection_component(document, inst_ids)
        model = "auto"
    data = pclib.export_component(comp, None, name=name, description=description, author=author,
                                  library=library, model=model, waveguide=waveguide)
    if sparams_wavelengths is not None and data["model"].get("physics"):
        m = pclib._model_from_physics(data["model"]["physics"])
        data["model"]["sparams"] = pclib.sample_sparams(m, sparams_wavelengths)
    p = Path(path)
    if not p.name.endswith(SUFFIX):
        p = p.with_name(p.name + SUFFIX)
    import json

    p.write_text(json.dumps(data, indent=1))
    return data
