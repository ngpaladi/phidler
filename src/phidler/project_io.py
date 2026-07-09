from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .custom_components import load_custom_components
from .fdtd_sim import SimulationConfig, SourceSpec
from .model.annotation import DEFAULT_ANNOTATION_COLOR, CalloutShape
from .model.document import EtchLayer, LayoutDocument, ProjectSettings
from .model.layers import LayerInfo
from .model.placed_instance import ArraySpec
from .pdk_catalog import ComponentSpec

PROJECT_VERSION = 1


def save_project(document: LayoutDocument, path: str) -> None:
    """Serializes the *recipe* needed to rebuild the document, not the
    gdsfactory objects themselves (those aren't serializable and don't need
    to be — replaying add_instance/add_route reconstructs them exactly)."""
    data = {
        "version": PROJECT_VERSION,
        "instances": [
            {
                "id": inst.id,
                "component_spec": inst.component_spec,
                "kwargs": inst.kwargs,
                "transform": asdict(document.get_transform(inst.id)),
                "array": asdict(inst.array),
            }
            for inst in document.instances.values()
        ],
        "routes": [
            {
                "id": route.id,
                "instance_id_a": route.instance_id_a,
                "port_name_a": route.port_name_a,
                "instance_id_b": route.instance_id_b,
                "port_name_b": route.port_name_b,
                "cross_section": route.cross_section,
                "goal_length_um": route.goal_length_um,
                "auto_match": route.auto_match,
                "meander_amplitude_um": route.meander_amplitude_um,
                "diagonal": route.diagonal,
            }
            for route in document.routes.values()
        ],
        "layers": [asdict(info) for info in document.layers.values()],
        # Notes + their callout drawings. asdict recurses into the CalloutShape
        # list, so the whole markup layer serializes in one shot.
        "annotations": [asdict(ann) for ann in document.annotations.values()],
        "reference_path": document.reference_path,
        "custom_component_paths": document.custom_component_paths,
        "project_settings": asdict(document.project_settings),
        # asdict recurses into the nested SourceSpec tuple too, so the whole
        # simulation set-up serializes in one shot. None when never configured.
        "simulation_config": (
            asdict(document.simulation_config) if document.simulation_config is not None else None
        ),
    }
    Path(path).write_text(json.dumps(data, indent=2))


def load_project(path: str, document: LayoutDocument, scene) -> dict[str, ComponentSpec]:
    """Replaces the contents of `document`/`scene` in place — they stay the
    same live objects already wired into the rest of the UI — by replaying
    the saved recipe through the normal add_instance/add_route APIs.
    Routes are replayed after all instances since they reference instance
    ids by name.

    Returns any custom components re-imported along the way (see below) so
    the caller can refresh the palette with them — project_io only owns
    `document`/`scene`, not the palette widget.
    """
    data = json.loads(Path(path).read_text())

    removed_inst_ids, removed_route_ids = document.clear_all()
    for inst_id in removed_inst_ids:
        scene.remove_instance_item(inst_id)
    for route_id in removed_route_ids:
        scene.remove_route_item(route_id)
    scene.clear_annotation_items()  # clear_all() emptied document.annotations; drop their items too
    scene.clear_reference_item()

    # Custom components only exist in the active PDK's cell registry
    # because load_custom_components() ran earlier *this process* — in a
    # fresh session gf.get_component(name) wouldn't resolve them at all,
    # so any instance using one must be re-imported before instance replay
    # below, not after (confirmed empirically: a project with a custom
    # part raised ValueError mid-replay and left the document empty,
    # since clear_all() had already run by the time the failure happened).
    custom_specs: dict[str, ComponentSpec] = {}
    for custom_path in data.get("custom_component_paths", []):
        try:
            result = load_custom_components(custom_path)
        except Exception:
            # the rest of the project can still load even if a custom
            # file moved/was deleted since this project was saved — any
            # instance that actually needed it will fail its own
            # add_instance call below and be reported then, rather than
            # failing the whole load over a missing file up front
            continue
        custom_specs.update(result.specs)
        document.record_custom_component_path(custom_path)

    max_id = 0
    for inst_data in data.get("instances", []):
        t = inst_data["transform"]
        array_data = inst_data.get("array")  # older saved projects predate arrays
        document.add_instance(
            inst_data["component_spec"],
            inst_data["kwargs"],
            x=t["x"],
            y=t["y"],
            rotation=t["rotation"],
            mirror=t["mirror"],
            mag=t.get("mag", 1.0),  # older saved projects predate the scale feature
            inst_id=inst_data["id"],
            array=ArraySpec(**array_data) if array_data else None,
        )
        scene.add_instance_item(inst_data["id"])
        max_id = max(max_id, inst_data["id"])

    for route_data in data.get("routes", []):
        document.add_route(
            route_data["instance_id_a"],
            route_data["port_name_a"],
            route_data["instance_id_b"],
            route_data["port_name_b"],
            route_data.get("cross_section", "strip"),
            route_id=route_data["id"],
            goal_length_um=route_data.get("goal_length_um"),
            auto_match=route_data.get("auto_match", False),
            meander_amplitude_um=route_data.get("meander_amplitude_um"),  # rebuild same geometry, no re-search
            diagonal=route_data.get("diagonal", False),
        )
        scene.add_route_item(route_data["id"])
        max_id = max(max_id, route_data["id"])

    for ann_data in data.get("annotations", []):  # absent in projects saved before notes -> []
        shapes = [
            CalloutShape(kind=s["kind"], points=[tuple(p) for p in s["points"]])
            for s in ann_data.get("shapes", [])
        ]
        document.add_annotation(
            ann_data["text"],
            ann_data["x"],
            ann_data["y"],
            shapes=shapes,
            color=ann_data.get("color", DEFAULT_ANNOTATION_COLOR),
            ann_id=ann_data["id"],
        )
        scene.add_annotation_item(ann_data["id"])
        max_id = max(max_id, ann_data["id"])

    for layer_data in data.get("layers", []):
        info = LayerInfo(**layer_data)
        document.layers[info.key] = info

    document.bump_id_counter(max_id + 1)

    settings_data = data.get("project_settings")
    if settings_data:
        settings_data = dict(settings_data)
        # asdict flattened the EtchLayer tuple to a list of dicts; rebuild them
        # (absent in projects saved before etch layers -> the () default).
        etch = settings_data.pop("etch_layers", None)
        if etch is not None:
            settings_data["etch_layers"] = tuple(EtchLayer(**e) for e in etch)
        document.project_settings = ProjectSettings(**settings_data)
    else:
        document.project_settings = ProjectSettings()

    sim_data = data.get("simulation_config")  # absent in projects saved before this feature
    if sim_data is not None:
        sources = tuple(SourceSpec(**s) for s in sim_data.get("sources", []))
        document.simulation_config = SimulationConfig(**{**sim_data, "sources": sources})
    else:
        document.simulation_config = None

    reference_path = data.get("reference_path")
    if reference_path:
        try:
            document.import_reference(reference_path)
        except Exception:
            # the rest of the project is still valid even if the backdrop
            # GDS moved/was deleted since this project was saved — don't
            # fail the whole load over a missing visual aid
            document.clear_reference()
        else:
            scene.show_reference()

    return custom_specs
