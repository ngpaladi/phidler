import math

from phidler.main_window import MainWindow
from phidler.model.document import Transform
from phidler.project_io import load_project, save_project


def _build_sample(win):
    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win.document.set_transform(b_id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    win.scene.items_by_inst[b_id].apply_transform(0.0, 20.0, 90.0, False)
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")
    return a_id, b_id


def test_save_and_load_round_trips_instances_and_routes(qapp, tmp_path):
    win = MainWindow()
    a_id, b_id = _build_sample(win)
    route_id = next(iter(win.document.routes))

    path = str(tmp_path / "test.phidler")
    save_project(win.document, path)

    win2 = MainWindow()
    load_project(path, win2.document, win2.scene)

    assert set(win2.document.instances.keys()) == {a_id, b_id}
    assert set(win2.document.routes.keys()) == {route_id}
    assert a_id in win2.scene.items_by_inst
    assert route_id in win2.scene.route_items

    t_b = win2.document.get_transform(b_id)
    assert math.isclose(t_b.x, 0.0, abs_tol=1e-6)
    assert math.isclose(t_b.y, 20.0, abs_tol=1e-6)
    assert math.isclose(t_b.rotation, 90.0, abs_tol=1e-6)

    route = win2.document.routes[route_id]
    assert route.instance_id_a == a_id
    assert route.instance_id_b == b_id


def test_load_into_existing_document_clears_previous_content(qapp, tmp_path):
    win = MainWindow()
    _build_sample(win)

    other = MainWindow()
    other._place_straight_waveguide()
    other_inst_id = next(iter(other.document.instances))
    path = str(tmp_path / "other.phidler")
    save_project(other.document, path)

    load_project(path, win.document, win.scene)

    assert other_inst_id in win.document.instances
    assert len(win.document.instances) == 1
    assert len(win.document.routes) == 0


def test_loaded_project_id_counter_avoids_collisions(qapp, tmp_path):
    win = MainWindow()
    _build_sample(win)
    path = str(tmp_path / "test.phidler")
    save_project(win.document, path)

    win2 = MainWindow()
    load_project(path, win2.document, win2.scene)

    existing_ids = set(win2.document.instances) | set(win2.document.routes)
    new_inst = win2.document.add_instance("straight", {"length": 5.0})
    assert new_inst.id not in existing_ids


def test_save_load_preserves_layer_color_override(qapp, tmp_path):
    win = MainWindow()
    win._place_straight_waveguide()
    win._on_layer_color_changed((1, 0), "#123456")

    path = str(tmp_path / "test.phidler")
    save_project(win.document, path)

    win2 = MainWindow()
    load_project(path, win2.document, win2.scene)
    assert win2.document.layers[(1, 0)].color == "#123456"


def test_export_after_load_produces_valid_gds(qapp, tmp_path):
    import gdsfactory as gf

    win = MainWindow()
    _build_sample(win)
    path = str(tmp_path / "test.phidler")
    save_project(win.document, path)

    win2 = MainWindow()
    load_project(path, win2.document, win2.scene)
    gds_path = tmp_path / "out.gds"
    win2.document.export_gds(str(gds_path))
    reimported = gf.import_gds(str(gds_path))
    assert not reimported.bbox().empty()


def _write_sample_reference_gds(tmp_path):
    import gdsfactory as gf

    c = gf.Component()
    c.add_polygon([(0, 0), (5, 0), (5, 5), (0, 5)], layer=(2, 0))
    path = str(tmp_path / "reference.gds")
    c.write_gds(path)
    return path


def test_save_and_load_round_trips_reference_path(qapp, tmp_path):
    win = MainWindow()
    _build_sample(win)
    ref_path = _write_sample_reference_gds(tmp_path)
    win.document.import_reference(ref_path)
    win.scene.show_reference()

    project_path = str(tmp_path / "test.phidler")
    save_project(win.document, project_path)

    win2 = MainWindow()
    load_project(project_path, win2.document, win2.scene)

    assert win2.document.reference_path == ref_path
    assert win2.document.reference is not None
    assert win2.scene.reference_item is not None
    assert win2.scene.reference_item.is_reference is True


def test_load_tolerates_missing_reference_file(qapp, tmp_path):
    """If the backdrop GDS moved/was deleted since the project was saved,
    loading the rest of the project must still succeed."""
    win = MainWindow()
    _build_sample(win)
    ref_path = str(tmp_path / "gone.gds")
    win.document.reference_path = ref_path  # simulate a saved-but-now-missing path

    project_path = str(tmp_path / "test.phidler")
    save_project(win.document, project_path)

    win2 = MainWindow()
    load_project(project_path, win2.document, win2.scene)  # must not raise

    assert win2.document.reference is None
    assert win2.scene.reference_item is None
    assert len(win2.document.instances) == 2  # the rest of the project still loaded


def test_save_and_load_round_trips_project_settings(qapp, tmp_path):
    from phidler.model.document import ProjectSettings

    win = MainWindow()
    _build_sample(win)
    win.document.project_settings = ProjectSettings(
        platform_name="Silicon Nitride (SiN)",
        core_index=2.0,
        clad_index=1.44,
        thickness_um=0.4,
        clad_thickness_um=3.5,
        wavelength_um=1.31,
        cross_section="nitride",
    )

    project_path = str(tmp_path / "test.phidler")
    save_project(win.document, project_path)

    win2 = MainWindow()
    load_project(project_path, win2.document, win2.scene)

    s = win2.document.project_settings
    assert s.platform_name == "Silicon Nitride (SiN)"
    assert math.isclose(s.core_index, 2.0)
    assert math.isclose(s.thickness_um, 0.4)
    assert math.isclose(s.clad_thickness_um, 3.5)
    assert math.isclose(s.wavelength_um, 1.31)
    assert s.cross_section == "nitride"


def test_save_and_load_round_trips_simulation_config(qapp, tmp_path):
    from phidler.fdtd_sim import SimulationConfig, SourceSpec

    win = MainWindow()
    _build_sample(win)
    win.document.simulation_config = SimulationConfig(
        wavelength_um=1.31,
        cell_size_um=0.05,
        run_time_fs=40.0,
        clad_index=1.46,
        use_numba=True,
        region_selected_only=True,
        sources=(
            SourceSpec(x_um=1.0, y_um=2.0, kind="dipole", wavelength_um=1.31),
            SourceSpec(x_um=3.0, y_um=4.0, kind="single_photon", wavelength_um=1.55, core_width_um=0.6),
        ),
        mode_core_width_um=0.7,
        mode_num_modes=3,
    )

    project_path = str(tmp_path / "test.phidler")
    save_project(win.document, project_path)

    win2 = MainWindow()
    load_project(project_path, win2.document, win2.scene)

    cfg = win2.document.simulation_config
    assert cfg is not None
    assert math.isclose(cfg.wavelength_um, 1.31)
    assert math.isclose(cfg.cell_size_um, 0.05)
    assert math.isclose(cfg.run_time_fs, 40.0)
    assert math.isclose(cfg.clad_index, 1.46)
    assert cfg.use_numba is True
    assert cfg.region_selected_only is True
    assert cfg.mode_num_modes == 3
    assert math.isclose(cfg.mode_core_width_um, 0.7)
    assert len(cfg.sources) == 2
    assert cfg.sources[0].kind == "dipole"
    assert math.isclose(cfg.sources[0].x_um, 1.0)
    assert cfg.sources[1].kind == "single_photon"
    assert math.isclose(cfg.sources[1].core_width_um, 0.6)


def test_load_project_without_simulation_config_leaves_it_none(qapp, tmp_path):
    """Projects saved before the simulation-config feature have no
    "simulation_config" key; loading must leave document.simulation_config None
    rather than raise or fabricate one."""
    win = MainWindow()
    _build_sample(win)
    project_path = str(tmp_path / "test.phidler")
    save_project(win.document, project_path)  # simulation_config is None -> serialized as null

    win2 = MainWindow()
    load_project(project_path, win2.document, win2.scene)
    assert win2.document.simulation_config is None


def test_load_project_file_missing_just_clad_thickness_field_uses_default(qapp, tmp_path):
    """A .phidler saved before clad_thickness_um existed has a
    "project_settings" object missing just that one key (unlike the
    "no project_settings key at all" case below) — ProjectSettings(**data)
    must fill in the dataclass default rather than raise a TypeError."""
    import json

    win = MainWindow()
    _build_sample(win)
    project_path = tmp_path / "old.phidler"
    save_project(win.document, str(project_path))
    data = json.loads(project_path.read_text())
    del data["project_settings"]["clad_thickness_um"]
    project_path.write_text(json.dumps(data))

    win2 = MainWindow()
    load_project(str(project_path), win2.document, win2.scene)  # must not raise
    assert math.isclose(win2.document.project_settings.clad_thickness_um, 2.0)


def test_load_old_project_file_without_settings_field_uses_defaults(qapp, tmp_path):
    """Backward compatibility: a .phidler saved before this feature existed
    has no "project_settings" key at all — loading it must not raise."""
    import json

    from phidler.model.document import ProjectSettings

    win = MainWindow()
    _build_sample(win)
    project_path = tmp_path / "old.phidler"
    save_project(win.document, str(project_path))
    data = json.loads(project_path.read_text())
    del data["project_settings"]
    project_path.write_text(json.dumps(data))

    win2 = MainWindow()
    load_project(str(project_path), win2.document, win2.scene)  # must not raise
    assert win2.document.project_settings == ProjectSettings()


def test_load_project_file_dispatches_py_to_script_importer(qapp, tmp_path):
    from phidler.export_script import export_python_script

    win = MainWindow()
    _build_sample(win)
    script_path = tmp_path / "layout.py"
    export_python_script(win.document, str(script_path))

    win2 = MainWindow()
    win2._load_project_file(str(script_path))
    assert len(win2.document.instances) == 2
    assert len(win2.document.routes) == 1


def test_opening_a_py_file_does_not_set_project_path(qapp, tmp_path):
    """Real safety concern: if project_path were set to the .py path, a
    later Save (Ctrl+S) would call save_project() and silently overwrite
    the user's Python script with JSON content."""
    from phidler.export_script import export_python_script

    win = MainWindow()
    _build_sample(win)
    script_path = tmp_path / "layout.py"
    export_python_script(win.document, str(script_path))

    win2 = MainWindow()
    win2._load_project_file(str(script_path))
    assert win2.project_path is None


def test_opening_a_phidler_file_does_set_project_path(qapp, tmp_path):
    win = MainWindow()
    _build_sample(win)
    project_path = tmp_path / "test.phidler"
    save_project(win.document, str(project_path))

    win2 = MainWindow()
    win2._load_project_file(str(project_path))
    assert win2.project_path == str(project_path)
