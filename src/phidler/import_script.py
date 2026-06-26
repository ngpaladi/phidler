from __future__ import annotations

import ast
import re
from pathlib import Path

from .custom_components import load_custom_components
from .model.document import LayoutDocument, ProjectSettings
from .model.placed_instance import ArraySpec
from .pdk_catalog import ComponentSpec

_INST_VAR_RE = re.compile(r"inst_(\d+)")
_SETTINGS_LINE_RE = re.compile(r"^(platform_name|core_index|clad_index|thickness_um|wavelength_um|cross_section):\s*(.+)$")
_SETTINGS_FLOAT_FIELDS = {"core_index", "clad_index", "thickness_um", "wavelength_um"}


class ScriptParseError(ValueError):
    """Raised when a .py file doesn't match the structure Phidler's own
    export_script.py generates closely enough to parse back. This is a
    pattern matcher for Phidler's own generated shape — simple literal
    edits (changing a kwarg value, a transform number, a cross_section
    string) are exactly what it's meant to tolerate; structural changes
    (loops, renamed-beyond-recognition variables, helper functions) are
    not, and raise this rather than silently reconstructing something
    wrong or silently dropping content."""


def _literal(node: ast.expr, context: str):
    try:
        return ast.literal_eval(node)
    except Exception as exc:
        raise ScriptParseError(f"expected a literal value for {context}, got: {ast.dump(node)}") from exc


def _is_attr_call(node: ast.expr, value_id: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == value_id
    )


def _is_add_ref_get_component(node: ast.expr) -> bool:
    return (
        _is_attr_call(node, "top", "add_ref")
        and len(node.args) == 1
        and _is_attr_call(node.args[0], "gf", "get_component")
        and len(node.args[0].args) == 1
        and isinstance(node.args[0].args[0], ast.Constant)
    )


def _meander_amplitude_from_steps(node: ast.expr) -> float | None:
    """Recover the meander amplitude from a route_single `steps` list literal:
    the first step's single dx/dy magnitude (see document._meander_steps)."""
    if not isinstance(node, ast.List) or not node.elts:
        return None
    first = node.elts[0]
    if not isinstance(first, ast.Dict) or not first.values:
        return None
    try:
        return abs(_literal(first.values[0], "route steps amplitude"))
    except ScriptParseError:
        return None


def _is_route_single_call(node: ast.expr) -> bool:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "route_single"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "routing"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "gf"
    ):
        return False
    return len(node.args) == 3 and all(_is_port_subscript(a) for a in node.args[1:])


def _is_route_bundle_all_angle_call(node: ast.expr) -> bool:
    """gf.routing.route_bundle_all_angle(top, [portA], [portB], ...) — the
    diagonal-route call export_script emits, with each port wrapped in a list."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "route_bundle_all_angle"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "routing"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "gf"
    ):
        return False
    return len(node.args) == 3 and all(
        isinstance(a, ast.List) and len(a.elts) == 1 and _is_port_subscript(a.elts[0]) for a in node.args[1:]
    )


def _is_port_subscript(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "ports"
        and isinstance(node.value.value, ast.Name)
        and isinstance(node.slice, ast.Constant)
    )


def _port_ref(node: ast.Subscript) -> tuple[str, str]:
    return node.value.value.id, node.slice.value


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def load_python_script(path: str, document: LayoutDocument, scene) -> dict[str, ComponentSpec]:
    """Reconstructs document/scene state directly from a Phidler-generated
    Python script's real code via AST parsing — deliberately not by
    executing the script and introspecting the result (gf.get_component(...)
    returns an opaque Component with an internal mangled cell name, e.g.
    "straight_L25__e0f5a055", losing the original component_spec="straight"
    + kwargs={"length": 25.0} entirely) and not via a separately-embedded
    data blob (which can silently drift from hand-edits to the executable
    code — the whole point of reading the real code is that editing
    `length=10.0` to `length=25.0` in the script and reopening it picks
    that up, because there's nothing else to go stale).

    Known, accepted loss versus a .phidler file: layer color/visibility
    overrides and the reference GDS backdrop path have no representation
    in the generated script at all and are not recovered — both reset to
    defaults. This is a deliberate scope choice (.py is an additional way
    to open a project, not a replacement for .phidler's full fidelity),
    not an oversight.
    """
    source = Path(path).read_text()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise ScriptParseError(f"{path} is not valid Python: {exc}") from exc

    custom_specs: dict[str, ComponentSpec] = {}
    custom_paths: list[str] = []
    pending_instances: dict[str, dict] = {}
    pending_routes: list[dict] = []

    # Only the module's direct top-level statements are inspected — not a
    # recursive ast.walk(). A statement nested inside a for/while/if/def is
    # invisible to a purely static parse in a way that matters: e.g.
    # `for i in range(3): inst = top.add_ref(...)` would match the same
    # assignment shape as a real instance, but only as ONE static node,
    # silently reconstructing 1 instance instead of the 3 actually created
    # at runtime (confirmed empirically — this is exactly the kind of
    # structural deviation that must raise, not silently under-reconstruct).
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # module docstring, handled separately via ast.get_docstring
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.If) and _is_main_guard(node):
            continue  # the GDS-writing `if __name__ == "__main__":` block — not inspected
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            if target.id == "top" and _is_attr_call(node.value, "gf", "Component"):
                continue
            if _is_add_ref_get_component(node.value):
                var_name = target.id
                inner = node.value.args[0]
                component_spec = inner.args[0].value
                kwargs = {kw.arg: _literal(kw.value, f"{var_name}.{kw.arg}") for kw in inner.keywords}
                # Array keywords (if any) live on the outer add_ref call, not on
                # the inner get_component — columns/rows/column_pitch/row_pitch.
                array_kwargs = {
                    kw.arg: _literal(kw.value, f"{var_name}.{kw.arg}")
                    for kw in node.value.keywords
                    if kw.arg in ("columns", "rows", "column_pitch", "row_pitch")
                }
                pending_instances.setdefault(var_name, {})
                pending_instances[var_name].update(
                    component_spec=component_spec, kwargs=kwargs, array_kwargs=array_kwargs
                )
                continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "dcplx_trans"
            and isinstance(node.targets[0].value, ast.Name)
            and _is_attr_call(node.value, "kdb", "DCplxTrans")
        ):
            var_name = node.targets[0].value.id
            if len(node.value.args) != 5:
                raise ScriptParseError(f"{var_name}.dcplx_trans: expected DCplxTrans(mag, rotation, mirror, x, y)")
            mag, rotation, mirror, x, y = (_literal(a, f"{var_name}.dcplx_trans arg") for a in node.value.args)
            pending_instances.setdefault(var_name, {})
            pending_instances[var_name].update(mag=mag, rotation=rotation, mirror=mirror, x=x, y=y)
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "activate":
                continue  # get_generic_pdk().activate()
            if isinstance(call.func, ast.Name) and call.func.id == "load_custom_components":
                if len(call.args) != 1:
                    raise ScriptParseError("load_custom_components(...) call must take exactly one path argument")
                custom_path = _literal(call.args[0], "load_custom_components path")
                result = load_custom_components(custom_path)
                custom_specs.update(result.specs)
                custom_paths.append(custom_path)
                continue
            if _is_route_single_call(call):
                a_var, port_a = _port_ref(call.args[1])
                b_var, port_b = _port_ref(call.args[2])
                cross_section = "strip"
                meander_amplitude_um = None
                for kw in call.keywords:
                    if kw.arg == "cross_section":
                        cross_section = _literal(kw.value, "route cross_section")
                    elif kw.arg == "steps":
                        meander_amplitude_um = _meander_amplitude_from_steps(kw.value)
                pending_routes.append(
                    {
                        "a_var": a_var,
                        "port_a": port_a,
                        "b_var": b_var,
                        "port_b": port_b,
                        "cross_section": cross_section,
                        "meander_amplitude_um": meander_amplitude_um,
                    }
                )
                continue
            if _is_route_bundle_all_angle_call(call):
                a_var, port_a = _port_ref(call.args[1].elts[0])
                b_var, port_b = _port_ref(call.args[2].elts[0])
                cross_section = "strip"
                for kw in call.keywords:
                    if kw.arg == "cross_section":
                        cross_section = _literal(kw.value, "route cross_section")
                pending_routes.append(
                    {
                        "a_var": a_var,
                        "port_a": port_a,
                        "b_var": b_var,
                        "port_b": port_b,
                        "cross_section": cross_section,
                        "diagonal": True,
                    }
                )
                continue
        raise ScriptParseError(
            f"unrecognized top-level statement at line {getattr(node, 'lineno', '?')}: {ast.dump(node)[:200]} "
            "— Phidler's script importer only understands the flat, simple statement shapes "
            "export_script.py generates (plus literal-value edits to them), not control flow, "
            "function/class definitions, or restructured code."
        )

    for var_name, data in pending_instances.items():
        if "component_spec" not in data:
            raise ScriptParseError(
                f"{var_name}.dcplx_trans was set but {var_name} was never created via "
                "top.add_ref(gf.get_component(...))"
            )

    removed_inst_ids, removed_route_ids = document.clear_all()
    for inst_id in removed_inst_ids:
        scene.remove_instance_item(inst_id)
    for route_id in removed_route_ids:
        scene.remove_route_item(route_id)
    scene.clear_reference_item()

    var_to_id: dict[str, int] = {}
    max_id = 0
    for var_name, data in pending_instances.items():
        match = _INST_VAR_RE.fullmatch(var_name)
        inst_id = int(match.group(1)) if match else document.next_id()
        array_kwargs = data.get("array_kwargs") or {}
        document.add_instance(
            data["component_spec"],
            data["kwargs"],
            x=data.get("x", 0.0),
            y=data.get("y", 0.0),
            rotation=data.get("rotation", 0.0),
            mirror=data.get("mirror", False),
            mag=data.get("mag", 1.0),
            inst_id=inst_id,
            array=ArraySpec(**array_kwargs) if array_kwargs else None,
        )
        scene.add_instance_item(inst_id)
        var_to_id[var_name] = inst_id
        max_id = max(max_id, inst_id)

    for route in pending_routes:
        a_id = var_to_id.get(route["a_var"])
        b_id = var_to_id.get(route["b_var"])
        if a_id is None or b_id is None:
            raise ScriptParseError(f"route_single(...) references an instance variable that was never created")
        amplitude = route.get("meander_amplitude_um")
        placed = document.add_route(
            a_id,
            route["port_a"],
            b_id,
            route["port_b"],
            route["cross_section"],
            auto_match=amplitude is not None,
            meander_amplitude_um=amplitude,
            diagonal=route.get("diagonal", False),
        )
        scene.add_route_item(placed.id)
        max_id = max(max_id, placed.id)

    document.bump_id_counter(max_id + 1)

    for custom_path in custom_paths:
        document.record_custom_component_path(custom_path)

    document.project_settings = _parse_settings_docstring(tree)

    return custom_specs


def _parse_settings_docstring(tree: ast.Module) -> ProjectSettings:
    docstring = ast.get_docstring(tree)
    if not docstring:
        return ProjectSettings()

    fields: dict[str, str] = {}
    for line in docstring.splitlines():
        match = _SETTINGS_LINE_RE.match(line.strip())
        if match:
            fields[match.group(1)] = match.group(2)

    if not fields:
        return ProjectSettings()

    kwargs = {}
    defaults = ProjectSettings()
    for key, raw_value in fields.items():
        if key in _SETTINGS_FLOAT_FIELDS:
            try:
                kwargs[key] = float(raw_value)
            except ValueError:
                kwargs[key] = getattr(defaults, key)
        else:
            kwargs[key] = raw_value
    return ProjectSettings(**kwargs)
