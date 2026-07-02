import sys
import types

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMenu, QMessageBox, QToolBar

from phidler.fdtd_sim import FdtdParams, SourceSpec
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument, ProjectSettings
from phidler.panels.fdtd_window import FdtdWindow


def _tiny_document() -> LayoutDocument:
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 1.5, "width": 0.5})
    doc.project_settings = ProjectSettings(core_index=3.45, clad_index=1.44, thickness_um=0.22, clad_thickness_um=1.0)
    return doc


def _pump_until(predicate, max_iters: int = 300, sleep_s: float = 0.02) -> bool:
    import time

    for _ in range(max_iters):
        QCoreApplication.processEvents()
        time.sleep(sleep_s)
        if predicate():
            return True
    return False


# -- construction ---------------------------------------------------------- #


def test_window_has_two_tabs(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    assert fdtd_win.centralWidget().count() == 2
    assert fdtd_win.centralWidget().tabText(0) == "Vertical Mode Profile"
    assert fdtd_win.centralWidget().tabText(1) == "Propagation (FDTD)"


def test_gpu_and_numba_checkboxes_feed_into_params(qapp):
    from phidler.fdtd_sim import gpu_available, numba_available

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    # GPU is off by default (its main-thread run can briefly freeze the UI), but
    # Numba is on by default when available — worker-thread, ~5x over NumPy.
    params = fdtd_win._current_params()
    assert params.use_gpu is False
    assert params.use_numba is numba_available()

    # The checkbox is enabled only when its backend is actually importable, so
    # a request can't silently no-op on the CPU.
    assert fdtd_win.run_gpu_check.isEnabled() == gpu_available()
    assert fdtd_win.run_numba_check.isEnabled() == numba_available()

    fdtd_win.run_gpu_check.setChecked(True)
    fdtd_win.run_numba_check.setChecked(True)
    params = fdtd_win._current_params()
    assert params.use_gpu is True and params.use_numba is True


def test_gpu_backend_name_tracks_availability_and_names_the_vendor(qapp):
    """GPU support is backend-agnostic (CuPy's CUDA build for NVIDIA or its ROCm
    build for AMD). gpu_backend_name() reports which is live, consistent with
    gpu_available(), and the GPU checkbox's tooltip names it when enabled."""
    from phidler.fdtd_sim import gpu_available, gpu_backend_name

    name = gpu_backend_name()
    # None exactly when no CuPy is importable; a vendor label otherwise.
    assert (name is None) == (not gpu_available())
    if name is not None:
        assert name in {"CUDA", "ROCm", "GPU"}

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    tip = fdtd_win.run_gpu_check.toolTip()
    if gpu_available():
        assert name in tip  # e.g. "...CuPy backend (CUDA)."
    else:
        # the disabled tip points at both wheels, not just CUDA
        assert "cupy-cuda12x" in tip and "cupy-rocm" in tip


def test_gpu_run_goes_through_the_worker_thread_via_subprocess(qapp):
    """A GPU-flagged run no longer blocks the main thread: it spawns a child
    process (own CUDA context, clean teardown) that the worker thread waits on,
    so the UI stays live. So a worker QThread *is* started and the result
    arrives asynchronously — the opposite of the old main-thread behaviour.

    CuPy's CUDA now lives in the child, isolated from the pytest process, so
    this is safe to run whether or not CuPy is installed (the child falls back
    to the CPU engine when CuPy is absent)."""
    import time

    from PySide6.QtTest import QTest

    win = MainWindow()
    inst = win.document.add_instance("straight", {"length": 2.0, "width": 0.5})
    win.scene.add_instance_item(inst.id)
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.15)  # tiny grid: stays under the warning threshold
    fdtd_win.run_time_spin.setValue(4.0)
    fdtd_win.run_gpu_check.setChecked(True)  # setChecked works even if the box is disabled
    fdtd_win._on_source_placement_requested(0.0, 0.0)

    fdtd_win._on_run_clicked()

    assert fdtd_win._fdtd_thread is not None  # async worker, not a main-thread block
    assert fdtd_win._last_result is None  # not ready synchronously

    deadline = time.time() + 60
    while fdtd_win._last_result is None and time.time() < deadline:
        QTest.qWait(50)
    assert fdtd_win._last_result is not None  # the child finished and shipped its result back
    assert fdtd_win._last_result.fields["field"]["Ez"].shape[3] == 1  # the mid-core plane


def test_simulate_selection_only_builds_a_region_from_the_selection(qapp):
    from phidler.model.document import Transform

    win = MainWindow()
    a = win.document.add_instance("straight", {"length": 30.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(b.id)
    win.document.set_transform(b.id, Transform(x=200.0, y=120.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[b.id].apply_transform(200.0, 120.0, 0.0, False)
    fdtd_win = FdtdWindow(win.document, win.view)

    assert fdtd_win._selected_region_um() is None  # nothing selected yet

    win.scene.items_by_inst[a.id].setSelected(True)  # select just the straight
    region = fdtd_win._selected_region_um()
    left, bottom, right, top = region
    assert right - left < 40 and top - bottom < 10  # the straight's box, not the whole sprawl
    assert left < 0 and bottom < 0  # includes the margin

    from phidler.fdtd_sim import FdtdParams, estimate_grid_cell_count

    p = FdtdParams(cell_size_um=0.06)
    assert estimate_grid_cell_count(win.document, p, region_um=region) < estimate_grid_cell_count(win.document, p) / 10


def test_field_image_is_placed_in_absolute_layout_coords(qapp):
    # from_gdsfactory centres the grid on 0, but the chip outline is in absolute
    # coords — so the field image must be shifted by the layout centre to line up.
    from phidler.fdtd_sim import FdtdParams, SourceSpec, build_simulation, run_simulation

    win = MainWindow()
    win.document.add_instance("straight", {"length": 40.0, "width": 0.5}, x=50.0, y=20.0)
    fdtd_win = FdtdWindow(win.document, win.view)
    bb = win.document.top.bbox()

    params = FdtdParams(cell_size_um=0.1, use_numba=True, sources=(SourceSpec(x_um=55.0, y_um=20.0),))
    sim = build_simulation(win.document, params)
    result = run_simulation(sim)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    cx, cy = fdtd_win._field_origin_um
    assert cx == pytest.approx((bb.left + bb.right) / 2)  # not 0 — the layout centre
    assert cy == pytest.approx((bb.bottom + bb.top) / 2)


def test_playback_speed_sets_the_frame_interval(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)

    fdtd_win.play_speed_combo.setCurrentIndex(2)  # 1×
    assert fdtd_win._play_interval_ms() == 100
    fdtd_win.play_speed_combo.setCurrentIndex(4)  # 4×
    assert fdtd_win._play_interval_ms() == 25
    fdtd_win.play_speed_combo.setCurrentIndex(0)  # 0.25×
    assert fdtd_win._play_interval_ms() == 400


def test_export_gif_writes_an_animated_gif(qapp, tmp_path):
    from PIL import Image

    from phidler.fdtd_sim import FdtdParams, SourceSpec, build_simulation, run_simulation

    win = MainWindow()
    win.document.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.run_view.resize(160, 90)  # grab() needs a real size

    params = FdtdParams(cell_size_um=0.1, run_time_fs=20.0, use_numba=True, sources=(SourceSpec(x_um=-4.0, y_um=0.0),))
    sim = build_simulation(win.document, params)
    result = run_simulation(sim)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)
    assert fdtd_win.save_gif_button.isEnabled()

    path = tmp_path / "sim.gif"
    n = fdtd_win._export_gif(str(path))
    assert n == result.fields["field"]["Ez"].shape[0]
    assert path.exists()
    img = Image.open(str(path))
    assert img.format == "GIF" and img.is_animated  # a real animation


def test_window_prefills_wavelength_from_project_settings(qapp):
    win = MainWindow()
    win.document.project_settings.wavelength_um = 1.31
    fdtd_win = FdtdWindow(win.document, win.view)
    assert fdtd_win.mode_wavelength_spin.value() == 1.31
    assert fdtd_win.run_wavelength_spin.value() == 1.31


def test_window_shows_clad_thickness_from_project_settings(qapp):
    win = MainWindow()
    win.document.project_settings.clad_thickness_um = 3.5
    fdtd_win = FdtdWindow(win.document, win.view)
    assert "3.500" in fdtd_win.run_clad_thickness_label.text()


# -- mode tab ---------------------------------------------------------------- #


def test_mode_solve_runs_through_real_threaded_wiring(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(_tiny_document(), win.view)
    fdtd_win.mode_core_width_spin.setValue(0.5)
    fdtd_win.mode_wavelength_spin.setValue(1.55)
    fdtd_win.document.project_settings.clad_thickness_um = 2.0

    fdtd_win._on_solve_mode_clicked()
    assert _pump_until(lambda: fdtd_win._mode_thread is not None and not fdtd_win._mode_thread.isRunning())

    assert "n_eff" in fdtd_win.mode_status_label.text()
    assert "Well confined" in fdtd_win.mode_status_label.text()
    assert fdtd_win.mode_solve_button.isEnabled()


def test_mode_solve_with_too_thin_cladding_reports_truncation(qapp):
    win = MainWindow()
    doc = _tiny_document()
    doc.project_settings.clad_thickness_um = 0.05
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.mode_core_width_spin.setValue(0.5)

    fdtd_win._on_solve_mode_clicked()
    assert _pump_until(lambda: fdtd_win._mode_thread is not None and not fdtd_win._mode_thread.isRunning())

    assert "Cladding may be too thin" in fdtd_win.mode_status_label.text()


# -- source placement --------------------------------------------------------- #


def test_place_source_button_arms_canvas_source_mode(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.place_source_button.setChecked(True)
    assert win.view.source_mode is True
    fdtd_win.place_source_button.setChecked(False)
    assert win.view.source_mode is False


def test_canvas_click_signal_adds_a_table_row_and_marker(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.run_wavelength_spin.setValue(1.55)

    win.view.source_placement_requested.emit(2.0, -1.0)

    assert fdtd_win.source_table.rowCount() == 1
    assert fdtd_win.source_table.item(0, 0).text() == "2.0000"
    assert fdtd_win.source_table.item(0, 1).text() == "-1.0000"
    assert len(win.view._source_markers) == 1


def test_remove_button_removes_row_and_marker(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)
    marker = fdtd_win._source_rows[0]["marker"]

    fdtd_win._on_remove_source_row(marker)

    assert fdtd_win.source_table.rowCount() == 0
    assert len(win.view._source_markers) == 0


def test_collect_source_specs_reflects_table_state(qapp):
    from phidler.panels.fdtd_window import _COL_CORE_WIDTH, _COL_KIND, _COL_PHOTON_COUNT

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(1.0, 2.0)
    fdtd_win.source_table.cellWidget(0, _COL_KIND).setCurrentText("single_photon")
    fdtd_win.source_table.item(0, _COL_PHOTON_COUNT).setText("3")
    fdtd_win.source_table.item(0, _COL_CORE_WIDTH).setText("0.6")

    specs = fdtd_win._collect_source_specs()
    assert len(specs) == 1
    spec = specs[0]
    assert spec.kind == "single_photon"
    assert spec.photon_count == 3
    assert spec.core_width_um == 0.6
    assert spec.x_um == 1.0
    assert spec.y_um == 2.0


def test_blank_source_cell_does_not_drop_the_whole_config(qapp):
    """A blank or half-typed source cell (the table is free text with no
    validator) must not sink the save. sync_config_to_document used to swallow
    the ValueError _collect_source_specs raised on an unparseable cell and keep
    the *previous* config — silently dropping every source. Now the one bad cell
    falls back to the field default and the source is still captured."""
    from phidler.panels.fdtd_window import _COL_WAVELENGTH, _COL_X

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(3.0, 4.0)
    # simulate a save landing mid-edit, with cells cleared to be retyped
    fdtd_win.source_table.item(0, _COL_WAVELENGTH).setText("")
    fdtd_win.source_table.item(0, _COL_X).setText("   ")

    fdtd_win.sync_config_to_document()

    config = win.document.simulation_config
    assert config is not None  # the config is not discarded...
    assert len(config.sources) == 1  # ...and the source survives
    assert config.sources[0].wavelength_um == 1.55  # blank -> field default
    assert config.sources[0].x_um == 0.0


def test_source_config_round_trips_through_a_saved_project(qapp, tmp_path):
    """End-to-end: a placed source is written into the .phidler file and comes
    back on load (the "sources aren't saved" path)."""
    from phidler.panels.fdtd_window import _COL_KIND
    from phidler.project_io import load_project, save_project

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win._fdtd_window = fdtd_win
    win.view.source_placement_requested.emit(1.0, 2.0)
    fdtd_win.source_table.cellWidget(0, _COL_KIND).setCurrentText("cherenkov")

    path = tmp_path / "proj.phidler"
    win._save_project_to(str(path))

    reopened = MainWindow()
    load_project(str(path), reopened.document, reopened.scene)
    config = reopened.document.simulation_config
    assert config is not None
    assert len(config.sources) == 1
    assert config.sources[0].kind == "cherenkov"
    assert config.sources[0].x_um == 1.0 and config.sources[0].y_um == 2.0


def test_placing_a_source_fills_in_the_equivalent_photon_energy(qapp):
    from phidler.fdtd_sim import photon_energy_ev_from_wavelength_um
    from phidler.panels.fdtd_window import _COL_ENERGY

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.run_wavelength_spin.setValue(1.55)
    win.view.source_placement_requested.emit(0.0, 0.0)

    energy = float(fdtd_win.source_table.item(0, _COL_ENERGY).text())
    assert energy == pytest.approx(photon_energy_ev_from_wavelength_um(1.55), abs=1e-3)


def test_editing_energy_column_updates_wavelength_column(qapp):
    from phidler.fdtd_sim import wavelength_um_from_photon_energy_ev
    from phidler.panels.fdtd_window import _COL_ENERGY, _COL_WAVELENGTH

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)

    fdtd_win.source_table.item(0, _COL_ENERGY).setText("0.8")
    wavelength = float(fdtd_win.source_table.item(0, _COL_WAVELENGTH).text())
    assert wavelength == pytest.approx(wavelength_um_from_photon_energy_ev(0.8), abs=1e-3)


def test_editing_wavelength_column_updates_energy_column(qapp):
    from phidler.fdtd_sim import photon_energy_ev_from_wavelength_um
    from phidler.panels.fdtd_window import _COL_ENERGY, _COL_WAVELENGTH

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)

    fdtd_win.source_table.item(0, _COL_WAVELENGTH).setText("1.31")
    energy = float(fdtd_win.source_table.item(0, _COL_ENERGY).text())
    assert energy == pytest.approx(photon_energy_ev_from_wavelength_um(1.31), abs=1e-3)


def test_scripted_kind_collects_the_script_text(qapp):
    from phidler.panels.fdtd_window import _COL_KIND, _COL_SCRIPT

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)
    fdtd_win.source_table.cellWidget(0, _COL_KIND).setCurrentText("scripted")
    fdtd_win.source_table.item(0, _COL_SCRIPT).setText("np.sin(2*np.pi*1.93e14*t)")

    specs = fdtd_win._collect_source_specs()
    assert specs[0].kind == "scripted"
    assert specs[0].script == "np.sin(2*np.pi*1.93e14*t)"


def test_run_simulation_with_a_scripted_source_completes(qapp):
    win = MainWindow()
    doc = _tiny_document()
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.1)
    fdtd_win.run_time_spin.setValue(3.0)
    win.view.source_placement_requested.emit(0.0, 0.0)
    fdtd_win.source_table.cellWidget(0, 2).setCurrentText("scripted")
    fdtd_win.source_table.item(0, 7).setText("np.sin(2*np.pi*1.93e14*t) * np.exp(-((t-3e-15)/1e-15)**2)")

    fdtd_win._on_run_clicked()
    assert _pump_until(lambda: fdtd_win._fdtd_thread is not None and not fdtd_win._fdtd_thread.isRunning())
    assert "Done" in fdtd_win.run_status_label.text()


def test_dipole_row_has_no_core_width(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)
    # default kind is "dipole"
    specs = fdtd_win._collect_source_specs()
    assert specs[0].kind == "dipole"
    assert specs[0].core_width_um is None


def test_closing_window_clears_markers_and_exits_source_mode(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)
    fdtd_win.place_source_button.setChecked(True)

    fdtd_win.close()

    assert win.view.source_mode is False
    assert len(win.view._source_markers) == 0


def test_sync_config_to_document_captures_controls_and_sources(qapp):
    from phidler.panels.fdtd_window import _COL_KIND

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.run_wavelength_spin.setValue(1.31)
    fdtd_win.run_cell_size_spin.setValue(0.05)
    fdtd_win.run_time_spin.setValue(40.0)
    fdtd_win.mode_num_modes_spin.setValue(3)
    win.view.source_placement_requested.emit(2.0, 3.0)
    fdtd_win.source_table.cellWidget(0, _COL_KIND).setCurrentText("dipole")

    fdtd_win.sync_config_to_document()

    cfg = win.document.simulation_config
    assert cfg is not None
    assert cfg.wavelength_um == pytest.approx(1.31)
    assert cfg.cell_size_um == pytest.approx(0.05)
    assert cfg.run_time_fs == pytest.approx(40.0)
    assert cfg.mode_num_modes == 3
    assert len(cfg.sources) == 1
    assert cfg.sources[0].x_um == pytest.approx(2.0)
    assert cfg.sources[0].y_um == pytest.approx(3.0)


def test_window_restores_saved_config_on_open(qapp):
    from phidler.fdtd_sim import SimulationConfig, SourceSpec
    from phidler.panels.fdtd_window import _COL_KIND, _COL_WAVELENGTH

    win = MainWindow()
    win.document.simulation_config = SimulationConfig(
        wavelength_um=1.31,
        cell_size_um=0.05,
        run_time_fs=40.0,
        sources=(SourceSpec(x_um=2.0, y_um=3.0, kind="single_photon", wavelength_um=1.55, core_width_um=0.6),),
        mode_num_modes=3,
    )

    fdtd_win = FdtdWindow(win.document, win.view)

    assert fdtd_win.run_wavelength_spin.value() == pytest.approx(1.31)
    assert fdtd_win.run_cell_size_spin.value() == pytest.approx(0.05)
    assert fdtd_win.run_time_spin.value() == pytest.approx(40.0)
    assert fdtd_win.mode_num_modes_spin.value() == 3
    assert fdtd_win.source_table.rowCount() == 1
    assert fdtd_win.source_table.cellWidget(0, _COL_KIND).currentText() == "single_photon"
    assert float(fdtd_win.source_table.item(0, _COL_WAVELENGTH).text()) == pytest.approx(1.55, abs=1e-3)
    # the source's canvas marker was recreated too
    assert len(win.view._source_markers) == 1
    # and it round-trips back out unchanged
    specs = fdtd_win._collect_source_specs()
    assert specs[0].kind == "single_photon"
    assert specs[0].core_width_um == pytest.approx(0.6)


def test_source_row_greys_out_parameters_irrelevant_to_its_kind(qapp):
    from PySide6.QtCore import Qt

    from phidler.panels.fdtd_window import (
        _COL_BETA,
        _COL_CORE_WIDTH,
        _COL_KIND,
        _COL_SCRIPT,
        _COL_WAVELENGTH,
    )

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.source_placement_requested.emit(0.0, 0.0)
    table = fdtd_win.source_table

    def enabled(col):
        return bool(table.item(0, col).flags() & Qt.ItemIsEnabled)

    # Default kind is "dipole": wavelength live; core width / script / β faded.
    assert enabled(_COL_WAVELENGTH)
    assert not enabled(_COL_CORE_WIDTH)
    assert not enabled(_COL_SCRIPT)
    assert not enabled(_COL_BETA)

    # single_photon lights up core width (still not script / β).
    table.cellWidget(0, _COL_KIND).setCurrentText("single_photon")
    assert enabled(_COL_CORE_WIDTH)
    assert not enabled(_COL_SCRIPT)

    # scripted: only the script cell is live; wavelength goes faded.
    table.cellWidget(0, _COL_KIND).setCurrentText("scripted")
    assert enabled(_COL_SCRIPT)
    assert not enabled(_COL_WAVELENGTH)
    assert not enabled(_COL_CORE_WIDTH)

    # cherenkov lights up β; switching back preserves the cell's value.
    table.cellWidget(0, _COL_KIND).setCurrentText("cherenkov")
    assert enabled(_COL_BETA)
    assert enabled(_COL_WAVELENGTH)
    assert table.item(0, _COL_BETA).text() == "0.8"  # value kept across kind changes


def test_save_project_persists_open_fdtd_window_config_end_to_end(qapp, tmp_path):
    """The literal user scenario: configure the (open) FDTD window, Ctrl+S,
    reopen the project in a fresh session, open the window — settings restored."""
    from phidler.panels.fdtd_window import _COL_KIND
    from phidler.project_io import load_project

    win = MainWindow()
    win.document.add_instance("straight", {"length": 5.0})
    win._open_fdtd_window()
    fdtd_win = win._fdtd_window
    fdtd_win.run_wavelength_spin.setValue(1.31)
    fdtd_win.run_cell_size_spin.setValue(0.05)
    win.view.source_placement_requested.emit(2.0, 3.0)
    fdtd_win.source_table.cellWidget(0, _COL_KIND).setCurrentText("dipole")

    path = str(tmp_path / "test.phidler")
    win._save_project_to(path)  # the real save path, including the sync-if-open wiring

    win2 = MainWindow()
    load_project(path, win2.document, win2.scene)
    fdtd_win2 = FdtdWindow(win2.document, win2.view)

    assert fdtd_win2.run_wavelength_spin.value() == pytest.approx(1.31)
    assert fdtd_win2.run_cell_size_spin.value() == pytest.approx(0.05)
    assert fdtd_win2.source_table.rowCount() == 1
    specs = fdtd_win2._collect_source_specs()
    assert specs[0].x_um == pytest.approx(2.0)
    assert specs[0].y_um == pytest.approx(3.0)


def test_save_does_not_fail_on_malformed_source_cell(qapp, tmp_path):
    """A half-typed source cell must not break the layout save — persisting the
    sim set-up is best-effort, the file save is not."""
    from phidler.panels.fdtd_window import _COL_X

    win = MainWindow()
    win.document.add_instance("straight", {"length": 5.0})
    win._open_fdtd_window()
    win.view.source_placement_requested.emit(2.0, 3.0)
    win._fdtd_window.source_table.item(0, _COL_X).setText("not a number")

    path = str(tmp_path / "test.phidler")
    win._save_project_to(path)  # must not raise

    assert win.project_path == path  # save completed


# -- running + playback -------------------------------------------------------- #


def test_run_simulation_through_real_threaded_wiring_completes_and_enables_playback(qapp):
    win = MainWindow()
    doc = _tiny_document()
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.1)
    fdtd_win.run_time_spin.setValue(3.0)

    fdtd_win._on_run_clicked()
    assert fdtd_win._fdtd_thread is not None
    assert _pump_until(lambda: not fdtd_win._fdtd_thread.isRunning())

    assert "Done" in fdtd_win.run_status_label.text()
    assert fdtd_win.frame_slider.isEnabled()
    assert fdtd_win.frame_slider.maximum() > 0


def test_progress_handler_switches_busy_to_determinate(qapp):
    """The bar starts busy (range 0–0); the first tick switches it to a
    determinate 0–100% and subsequent ticks set the percentage."""
    win = MainWindow()
    fdtd_win = FdtdWindow(_tiny_document(), win.view)

    fdtd_win.run_progress.setRange(0, 0)  # busy, as _on_run_clicked leaves it
    fdtd_win._on_fdtd_progress(0, 200)
    assert fdtd_win.run_progress.maximum() == 100  # left busy mode
    fdtd_win._on_fdtd_progress(100, 200)
    assert fdtd_win.run_progress.value() == 50
    fdtd_win._on_fdtd_progress(200, 200)
    assert fdtd_win.run_progress.value() == 100


def test_run_shows_progress_bar_reaches_100_then_hides(qapp):
    """End-to-end: the real photonfdtd progress callback flows through the
    worker's progress signal to the bar, which is shown while running, fills to
    100%, and is hidden on completion."""
    win = MainWindow()
    fdtd_win = FdtdWindow(_tiny_document(), win.view)
    fdtd_win.run_cell_size_spin.setValue(0.1)
    fdtd_win.run_time_spin.setValue(3.0)

    fdtd_win._on_run_clicked()
    assert fdtd_win._fdtd_thread is not None
    assert not fdtd_win.run_progress.isHidden()  # shown as soon as the run starts

    assert _pump_until(lambda: not fdtd_win._fdtd_thread.isRunning())
    qapp.processEvents()  # drain the final queued progress + finished signals

    assert fdtd_win.run_progress.maximum() == 100  # became determinate from real ticks
    assert fdtd_win.run_progress.value() == 100    # reached 100%
    assert fdtd_win.run_progress.isHidden()        # hidden once done


def test_run_simulation_on_empty_layout_shows_warning_not_crash(qapp, monkeypatch):
    win = MainWindow()
    fdtd_win = FdtdWindow(LayoutDocument(), win.view)

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(True))
    fdtd_win._on_run_clicked()

    assert warned
    assert fdtd_win._fdtd_thread is None


def test_run_simulation_warns_before_a_slow_estimated_run(qapp, monkeypatch):
    win = MainWindow()
    doc = _tiny_document()
    inst_id = next(iter(doc.instances))
    doc.update_instance_params(inst_id, {"length": 100.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.02)
    fdtd_win.run_time_spin.setValue(500.0)

    asked = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: (asked.append(True), QMessageBox.No)[1]
    )
    fdtd_win._on_run_clicked()

    assert asked
    assert fdtd_win._fdtd_thread is None  # declined, so no run started


def test_play_toggle_starts_and_stops_the_timer(qapp):
    win = MainWindow()
    doc = _tiny_document()
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.1)
    fdtd_win.run_time_spin.setValue(3.0)
    fdtd_win._on_run_clicked()
    assert _pump_until(lambda: not fdtd_win._fdtd_thread.isRunning())

    fdtd_win.play_button.setChecked(True)
    assert fdtd_win._play_timer.isActive()
    fdtd_win.play_button.setChecked(False)
    assert not fdtd_win._play_timer.isActive()


def test_frame_advance_wraps_around_at_the_end(qapp):
    win = MainWindow()
    doc = _tiny_document()
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.1)
    fdtd_win.run_time_spin.setValue(3.0)
    fdtd_win._on_run_clicked()
    assert _pump_until(lambda: not fdtd_win._fdtd_thread.isRunning())

    fdtd_win.frame_slider.setValue(fdtd_win.frame_slider.maximum())
    fdtd_win._advance_frame()
    assert fdtd_win.frame_slider.value() == 0


# -- MainWindow wiring --------------------------------------------------------- #


def test_main_window_has_simulate_toolbar_button(qapp):
    win = MainWindow()
    assert win.fdtd_window_action.text() == "Simulate"
    # It's a toolbar action now, not under a Simulate menu. The controls are
    # split across several toolbars, so look across all of them.
    toolbar_actions = [a for tb in win.findChildren(QToolBar) for a in tb.actions()]
    assert win.fdtd_window_action in toolbar_actions
    assert all(m.title() != "&Simulate" for m in win.menuBar().findChildren(QMenu))


def test_main_window_opens_fdtd_window_lazily_and_reuses_it(qapp):
    win = MainWindow()
    assert win._fdtd_window is None
    win._open_fdtd_window()
    first = win._fdtd_window
    assert isinstance(first, FdtdWindow)
    win._open_fdtd_window()
    assert win._fdtd_window is first


def test_main_window_shows_a_warning_when_fdtd_extras_are_missing(qapp, monkeypatch):
    """Simulates photonfdtd/matplotlib not being installed by making the
    module import succeed but the FdtdWindow name missing from it — the
    same ImportError shape `from phidler.panels.fdtd_window import
    FdtdWindow` would raise if the module's own internal matplotlib import
    failed and propagated up."""
    fake_module = types.ModuleType("phidler.panels.fdtd_window")
    monkeypatch.setitem(sys.modules, "phidler.panels.fdtd_window", fake_module)

    win = MainWindow()
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(True))

    win._open_fdtd_window()

    assert warned
    assert win._fdtd_window is None
