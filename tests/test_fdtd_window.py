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
    # Off by default — the plain NumPy engine, no optional deps required.
    params = fdtd_win._current_params()
    assert params.use_gpu is False and params.use_numba is False

    # The checkbox is enabled only when its backend is actually importable, so
    # a request can't silently no-op on the CPU.
    assert fdtd_win.run_gpu_check.isEnabled() == gpu_available()
    assert fdtd_win.run_numba_check.isEnabled() == numba_available()

    fdtd_win.run_gpu_check.setChecked(True)
    fdtd_win.run_numba_check.setChecked(True)
    params = fdtd_win._current_params()
    assert params.use_gpu is True and params.use_numba is True


def test_gpu_run_executes_synchronously_not_in_a_worker_thread(qapp):
    """cupy's CUDA context can't survive a Qt worker thread (crash/hang at
    teardown), so a GPU-flagged run executes on the main thread — no worker
    QThread, and the result is ready by the time _on_run_clicked returns.

    Skipped when CuPy is actually installed: this runs a real FDTD solve, and a
    real CUDA run inside the shared pytest process leaves device state that
    destabilises later tests. With CuPy absent (CI, most dev machines) the GPU
    flag falls back to NumPy on the main thread, which exercises the same
    synchronous branch this test checks."""
    import pytest

    from phidler.fdtd_sim import gpu_available

    if gpu_available():
        pytest.skip("CuPy present — a real CUDA run in the test process would destabilise the suite")

    win = MainWindow()
    inst = win.document.add_instance("straight", {"length": 2.0, "width": 0.5})
    win.scene.add_instance_item(inst.id)
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.run_cell_size_spin.setValue(0.15)  # tiny grid: stays under the warning threshold
    fdtd_win.run_time_spin.setValue(4.0)
    fdtd_win.run_gpu_check.setChecked(True)  # setChecked works even if the box is disabled
    fdtd_win._on_source_placement_requested(0.0, 0.0)

    fdtd_win._on_run_clicked()

    assert fdtd_win._fdtd_thread is None  # no background worker for a GPU run
    assert fdtd_win._last_result is not None  # finished synchronously


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
    # It's a toolbar action now, not under a Simulate menu.
    assert win.fdtd_window_action in win.findChild(QToolBar).actions()
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
