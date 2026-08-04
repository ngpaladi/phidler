"""Measurement probes: canvas placement (with joint snapping), intensity
readout from a run, the live graph overlay, and export."""
import math

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from phidler.canvas.scene import LayoutScene
from phidler.canvas.view import LayoutView
from phidler.fdtd_sim import FdtdParams, ProbeSpec, SourceSpec, probe_intensity_trace
from phidler.main_window import MainWindow
from phidler.model.document import LayoutDocument, ProjectSettings
from phidler.panels.fdtd_window import (
    _PCOL_GRAPH,
    _PCOL_I_FRAME,
    _PCOL_I_MEAN,
    _PCOL_I_PEAK,
    _PCOL_X,
    _PCOL_Y,
    FdtdWindow,
)


def _click(view, scene_pt: QPointF) -> None:
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.mapFromScene(scene_pt))


def _run_a_tiny_sim(document, **kwargs):
    """A real (small) FDTD run, so probe sampling is exercised against a genuine
    photonfdtd result rather than a hand-built stub."""
    from phidler.fdtd_sim import build_simulation, run_simulation

    params = FdtdParams(
        cell_size_um=0.1, run_time_fs=20.0, use_numba=True,
        sources=(SourceSpec(x_um=-4.0, y_um=0.0),), **kwargs,
    )
    sim = build_simulation(document, params)
    return params, sim, run_simulation(sim)


# -- canvas placement ------------------------------------------------------- #


def test_click_in_probe_mode_emits_placement_request(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.probe_placement_requested.connect(lambda x, y: received.append((x, y)))

    view.set_probe_mode(True)
    _click(view, QPointF(4.0, -2.0))

    assert len(received) == 1
    assert math.isclose(received[0][0], 4.0, abs_tol=1e-6)
    assert math.isclose(received[0][1], -2.0, abs_tol=1e-6)


def test_probe_click_snaps_to_a_waveguide_joint(qapp):
    """The headline behaviour: a click near a port lands on the joint exactly,
    not wherever the mouse happened to be. straight's ports are o1 at local
    (0,0) and o2 at (10,0)."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(a.id)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.probe_placement_requested.connect(lambda x, y: received.append((x, y)))

    view.set_probe_mode(True)
    _click(view, QPointF(10.3, 0.3))  # near o2, not on it

    assert len(received) == 1
    x, y = received[0]
    assert math.isclose(x, 10.0, abs_tol=1e-6)
    assert math.isclose(y, 0.0, abs_tol=1e-6)


def test_probe_click_far_from_any_joint_is_left_where_clicked(qapp):
    """Snapping must not teleport a deliberate mid-waveguide probe to a port."""
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    scene = LayoutScene(doc)
    scene.add_instance_item(a.id)
    view = LayoutView(scene)
    view.resize(400, 400)
    view.show()

    received = []
    view.probe_placement_requested.connect(lambda x, y: received.append((x, y)))

    view.set_probe_mode(True)
    _click(view, QPointF(5.0, 0.0))  # mid-span, far from both ports

    assert math.isclose(received[0][0], 5.0, abs_tol=1e-6)


def test_probe_mode_is_exclusive_with_the_other_click_modes(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)

    view.set_source_mode(True)
    view.set_probe_mode(True)
    assert view.probe_mode is True and view.source_mode is False

    view.set_measure_mode(True)
    assert view.probe_mode is False

    view.set_probe_mode(True)
    view.set_routing_mode(True)
    assert view.probe_mode is False


def test_escape_leaves_probe_mode(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    view.set_probe_mode(True)
    assert view.cancel_current_action() is True
    assert view.probe_mode is False


def test_probe_markers_accumulate_and_clear(qapp):
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    view = LayoutView(scene)
    m1 = view.add_probe_marker(1.0, 2.0)
    view.add_probe_marker(3.0, 4.0)
    assert len(view._probe_markers) == 2

    view.remove_probe_marker(m1)
    assert len(view._probe_markers) == 1
    view.clear_probe_markers()
    assert view._probe_markers == []


# -- table wiring ----------------------------------------------------------- #


def test_place_probe_button_arms_canvas_probe_mode(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.place_probe_button.setChecked(True)
    assert win.view.probe_mode is True
    fdtd_win.place_probe_button.setChecked(False)
    assert win.view.probe_mode is False


def test_arming_one_placement_tool_disarms_the_other(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)

    fdtd_win.place_source_button.setChecked(True)
    fdtd_win.place_probe_button.setChecked(True)
    assert fdtd_win.place_source_button.isChecked() is False
    assert win.view.probe_mode is True

    fdtd_win.place_source_button.setChecked(True)
    assert fdtd_win.place_probe_button.isChecked() is False
    assert win.view.source_mode is True


def test_canvas_click_signal_adds_a_probe_row_and_marker(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)

    win.view.probe_placement_requested.emit(2.5, -1.5)

    assert fdtd_win.probe_table.rowCount() == 1
    assert fdtd_win.probe_table.item(0, _PCOL_X).text() == "2.5000"
    assert fdtd_win.probe_table.item(0, _PCOL_Y).text() == "-1.5000"
    assert len(win.view._probe_markers) == 1
    # No run yet, so the readouts are blank rather than a misleading zero.
    assert fdtd_win.probe_table.item(0, _PCOL_I_FRAME).text() == "—"


def test_remove_button_removes_probe_row_and_marker(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)
    marker = fdtd_win._probe_rows[0]["marker"]

    fdtd_win._on_remove_probe_row(marker)

    assert fdtd_win.probe_table.rowCount() == 0
    assert len(win.view._probe_markers) == 0


def test_editing_probe_xy_moves_its_canvas_marker(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.probe_placement_requested.emit(1.0, 1.0)

    fdtd_win.probe_table.item(0, _PCOL_X).setText("7.5")

    pos = fdtd_win._probe_rows[0]["marker"].pos()
    assert pos.x() == pytest.approx(7.5)
    assert pos.y() == pytest.approx(1.0)


def test_intensity_cells_are_read_only(qapp):
    """They're measurements, not inputs — a typed value must not masquerade
    as a result."""
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)
    for col in (_PCOL_I_FRAME, _PCOL_I_MEAN, _PCOL_I_PEAK):
        assert not (fdtd_win.probe_table.item(0, col).flags() & Qt.ItemIsEditable)


# -- measurement ------------------------------------------------------------ #


def test_probe_trace_is_per_frame_intensity_inside_the_grid(qapp):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.project_settings = ProjectSettings(core_index=3.45, clad_index=1.44, thickness_um=0.22)
    _params, sim, result = _run_a_tiny_sim(doc)

    trace = probe_intensity_trace(sim, result, 0.0, 0.0)

    assert trace is not None
    assert trace.shape == (result.fields["field"]["Ez"].shape[0],)
    assert np.all(trace >= 0.0)  # |Ez|², never negative
    assert trace.max() > 0.0     # the pulse actually reached the middle


def test_probe_outside_the_grid_reports_none(qapp):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    _params, sim, result = _run_a_tiny_sim(doc)

    assert probe_intensity_trace(sim, result, 5000.0, 5000.0) is None


def test_probe_respects_the_absolute_layout_origin(qapp):
    """The grid is centred on 0 but probes are placed in absolute layout coords,
    so the same physical point must sample identically under both framings."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=100.0, y=50.0)
    _params, sim, result = _run_a_tiny_sim(doc)
    bb = doc.top.bbox()
    origin = ((bb.left + bb.right) / 2, (bb.bottom + bb.top) / 2)

    centred = probe_intensity_trace(sim, result, 0.0, 0.0)
    absolute = probe_intensity_trace(sim, result, origin[0], origin[1], origin_um=origin)

    assert absolute is not None
    assert np.allclose(centred, absolute)


def test_run_fills_in_probe_intensities(qapp):
    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    doc.project_settings = ProjectSettings(core_index=3.45, clad_index=1.44, thickness_um=0.22)
    fdtd_win = FdtdWindow(doc, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    for col in (_PCOL_I_FRAME, _PCOL_I_MEAN, _PCOL_I_PEAK):
        text = fdtd_win.probe_table.item(0, col).text()
        assert text not in ("—", "off grid")
        assert float(text) >= 0.0
    assert fdtd_win._probe_rows[0]["trace"] is not None


def test_probe_added_after_a_run_measures_it_without_rerunning(qapp):
    """Probes are post-processing over the recorded movie, so dropping one after
    a run must fill in immediately."""
    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    win.view.probe_placement_requested.emit(0.0, 0.0)  # placed *after* the run

    assert fdtd_win._probe_rows[0]["trace"] is not None
    assert float(fdtd_win.probe_table.item(0, _PCOL_I_PEAK).text()) > 0.0


def test_moving_a_probe_resamples_the_existing_run(qapp):
    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)
    before = fdtd_win._probe_rows[0]["trace"].copy()

    fdtd_win.probe_table.item(0, _PCOL_X).setText("3.0")  # a different cell

    assert not np.allclose(before, fdtd_win._probe_rows[0]["trace"])


def test_frame_readout_follows_the_playback_slider(qapp):
    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    trace = fdtd_win._probe_rows[0]["trace"]
    peak_frame = int(np.argmax(trace))
    fdtd_win.frame_slider.setValue(peak_frame)

    shown = float(fdtd_win.probe_table.item(0, _PCOL_I_FRAME).text())
    assert shown == pytest.approx(float(trace[peak_frame]), rel=1e-3)
    # The graph playhead tracks the same frame.
    assert fdtd_win.run_view._probe_graph_frame == peak_frame


# -- graphs and export ------------------------------------------------------ #


def test_graph_checkbox_controls_the_field_view_overlay(qapp):
    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    assert len(fdtd_win.run_view._probe_graphs) == 1  # graphed by default

    fdtd_win.probe_table.cellWidget(0, _PCOL_GRAPH).setChecked(False)
    assert fdtd_win.run_view._probe_graphs == []

    fdtd_win.probe_table.cellWidget(0, _PCOL_GRAPH).setChecked(True)
    assert len(fdtd_win.run_view._probe_graphs) == 1


def test_export_graphs_writes_a_standalone_png(qapp, tmp_path):
    from PIL import Image

    from phidler.panels.fdtd_window import render_probe_graphs_image

    trace = np.linspace(0.0, 1.0, 32) ** 2
    image = render_probe_graphs_image([{"label": "P1", "color": "#00e676", "trace": trace}], 5)
    path = tmp_path / "graphs.png"
    assert image.save(str(path), "PNG")

    with Image.open(str(path)) as img:
        assert img.format == "PNG"
        assert img.width > 0 and img.height > 0


def test_export_graphs_csv_has_time_and_one_column_per_probe(qapp, tmp_path):
    import csv

    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)
    win.view.probe_placement_requested.emit(2.0, 0.0)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    path = tmp_path / "probes.csv"
    graphs = fdtd_win._probe_graph_data()
    fdtd_win._export_graphs_csv(str(path), graphs)

    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["time_fs", "P1_intensity", "P2_intensity"]
    assert len(rows) - 1 == len(graphs[0]["trace"])
    # Time runs from 0 to the run duration, not a frame index.
    assert float(rows[1][0]) == 0.0
    assert float(rows[-1][0]) == pytest.approx(params.resolved_run_time_fs())


def test_gif_export_includes_the_graphs_in_the_canvas(qapp, tmp_path):
    """'Part of the sim canvas': graphs are painted into the field view's
    foreground, so a grab (and hence the GIF) carries them."""
    win = MainWindow()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 10.0, "width": 0.5})
    fdtd_win = FdtdWindow(doc, win.view)
    fdtd_win.run_view.resize(320, 200)
    win.view.probe_placement_requested.emit(0.0, 0.0)

    params, sim, result = _run_a_tiny_sim(doc)
    fdtd_win._last_params = params
    fdtd_win._region_um = None
    fdtd_win._on_fdtd_finished(sim, result, 1.0)

    with_graphs = fdtd_win.run_view.grab().toImage()
    fdtd_win.probe_table.cellWidget(0, _PCOL_GRAPH).setChecked(False)
    without_graphs = fdtd_win.run_view.grab().toImage()

    assert with_graphs != without_graphs  # the graph really is drawn on the canvas


# -- persistence ------------------------------------------------------------ #


def test_probes_round_trip_through_a_saved_project(qapp, tmp_path):
    from phidler.project_io import load_project, save_project

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.probe_placement_requested.emit(3.5, -2.25)
    win.view.probe_placement_requested.emit(8.0, 1.0)
    fdtd_win.probe_table.cellWidget(1, _PCOL_GRAPH).setChecked(False)
    fdtd_win.sync_config_to_document()

    path = tmp_path / "probes.phidler"
    save_project(win.document, str(path))

    reloaded = MainWindow()
    load_project(str(path), reloaded.document, reloaded.scene)
    probes = reloaded.document.simulation_config.probes

    assert len(probes) == 2
    assert probes[0] == ProbeSpec(x_um=3.5, y_um=-2.25, graph=True)
    assert probes[1].graph is False


def test_window_restores_saved_probes_on_open(qapp):
    from phidler.fdtd_sim import SimulationConfig

    win = MainWindow()
    win.document.simulation_config = SimulationConfig(
        probes=(ProbeSpec(x_um=1.0, y_um=2.0), ProbeSpec(x_um=4.0, y_um=5.0, graph=False)),
    )
    fdtd_win = FdtdWindow(win.document, win.view)

    assert fdtd_win.probe_table.rowCount() == 2
    assert fdtd_win.probe_table.item(1, _PCOL_X).text() == "4.0000"
    assert fdtd_win.probe_table.cellWidget(1, _PCOL_GRAPH).isChecked() is False
    assert len(win.view._probe_markers) == 2


def test_a_project_saved_before_probes_existed_still_loads(qapp, tmp_path):
    """The probes field is new; an older .phidler has no such key."""
    import json

    from phidler.project_io import load_project, save_project

    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.sync_config_to_document()
    path = tmp_path / "old.phidler"
    save_project(win.document, str(path))

    data = json.loads(path.read_text())
    del data["simulation_config"]["probes"]  # as an older phidler wrote it
    path.write_text(json.dumps(data))

    reloaded = MainWindow()
    load_project(str(path), reloaded.document, reloaded.scene)
    assert reloaded.document.simulation_config.probes == ()


def test_closing_the_window_clears_probe_markers_and_exits_probe_mode(qapp):
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.probe_placement_requested.emit(0.0, 0.0)
    fdtd_win.place_probe_button.setChecked(True)

    fdtd_win.close()

    assert win.view.probe_mode is False
    assert win.view._probe_markers == []


def test_close_still_captures_config_after_the_closeevent_merge(qapp):
    """FdtdWindow used to define closeEvent twice, so the config capture never
    ran on close. Both halves now live in one method — guard that."""
    win = MainWindow()
    fdtd_win = FdtdWindow(win.document, win.view)
    win.view.probe_placement_requested.emit(6.0, 7.0)
    win.document.simulation_config = None

    fdtd_win.close()

    assert win.document.simulation_config is not None
    assert win.document.simulation_config.probes == (ProbeSpec(x_um=6.0, y_um=7.0),)
