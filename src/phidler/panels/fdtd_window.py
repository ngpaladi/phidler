from __future__ import annotations

import time

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from phidler.canvas.view import LayoutView
from phidler.fdtd_sim import (
    DISCLAIMER,
    FdtdParams,
    ModeProfileParams,
    SourceSpec,
    build_mode_solver,
    build_simulation,
    estimate_grid_cell_count,
    estimate_run_seconds,
    mode_confinement,
    nearest_z_index,
    photon_energy_ev_from_wavelength_um,
    run_simulation,
    solve_mode_profile,
    wavelength_um_from_photon_energy_ev,
)
from phidler.model.document import LayoutDocument, shapes_for_cell

# Estimated-time threshold above which the user is asked to confirm before
# a run starts — true 3D propagation is much more expensive per cell than
# the old quasi-2D path it replaced, so this is a time estimate (from
# fdtd_sim.estimate_run_seconds's empirical calibration), not a bare cell
# count: time is what the user actually cares about before deciding to wait.
_RUN_TIME_WARNING_SECONDS = 5.0

_TABLE_COLUMNS = [
    "X (µm)",
    "Y (µm)",
    "Kind",
    "Wavelength (µm)",
    "Energy (eV)",
    "Photon count",
    "Core width (µm)",
    "Script (kind=scripted)",
    "",
]
_COL_X, _COL_Y, _COL_KIND, _COL_WAVELENGTH, _COL_ENERGY, _COL_PHOTON_COUNT, _COL_CORE_WIDTH, _COL_SCRIPT, _COL_REMOVE = range(9)


class ModeWorker(QObject):
    """Thin QThread wrapper around build_mode_solver+solve_mode_profile —
    same split as FdtdWorker below: the actual compute lives in
    fdtd_sim.py with no Qt/threading involved, fully unit-tested there;
    this only moves the call off the GUI thread."""

    finished = Signal(object, float)  # ModeResult, elapsed_seconds
    failed = Signal(str)

    def __init__(self, settings, params: ModeProfileParams) -> None:
        super().__init__()
        self.settings = settings
        self.params = params

    def run(self) -> None:
        try:
            t0 = time.time()
            solver = build_mode_solver(self.settings, self.params)
            result = solve_mode_profile(solver)
            elapsed = time.time() - t0
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result, elapsed)


class FdtdWorker(QObject):
    """Runs build_simulation()+run_simulation() off the main thread — true
    3D propagation is genuinely expensive (calibrated empirically at
    ~6e-8 s/cell-step on dev hardware), so this must not block the UI."""

    finished = Signal(object, object, float)  # Simulation, Result, elapsed_seconds
    failed = Signal(str)

    def __init__(self, document: LayoutDocument, params: FdtdParams) -> None:
        super().__init__()
        self.document = document
        self.params = params

    def run(self) -> None:
        try:
            t0 = time.time()
            sim = build_simulation(self.document, self.params)
            result = run_simulation(sim)
            elapsed = time.time() - t0
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(sim, result, elapsed)


class FdtdWindow(QMainWindow):
    """Top-level (non-modal) window for FDTD simulation — replaces the
    original docked FdtdPanel. Two tabs: a fast vertical mode-profile
    solver (the tool that makes cladding thickness matter — see
    fdtd_sim.mode_confinement) and full 3D propagation with click-placed
    sources and movie playback."""

    def __init__(self, document: LayoutDocument, view: LayoutView, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FDTD Simulation")
        self.document = document
        self.view = view

        self._mode_thread = None
        self._mode_worker = None
        self._fdtd_thread = None
        self._fdtd_worker = None

        self._source_rows: list[dict] = []  # {"marker":..., row index tracked via table}
        self._syncing_wavelength_energy = False
        self._last_sim = None
        self._last_result = None
        self._last_params = None
        self._chip_outline_drawn = False

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(100)
        self._play_timer.timeout.connect(self._advance_frame)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_mode_tab(), "Vertical Mode Profile")
        tabs.addTab(self._build_propagation_tab(), "Propagation (FDTD)")

        self.view.source_placement_requested.connect(self._on_source_placement_requested)
        self.resize(700, 800)

    # -- mode profile tab --------------------------------------------------

    def _build_mode_tab(self) -> QWidget:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        widget = QWidget()
        layout = QVBoxLayout(widget)

        disclaimer = QLabel(DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(disclaimer)

        form = QFormLayout()
        self.mode_wavelength_spin = QDoubleSpinBox()
        self.mode_wavelength_spin.setDecimals(3)
        self.mode_wavelength_spin.setRange(0.1, 10.0)
        self.mode_wavelength_spin.setValue(self.document.project_settings.wavelength_um)
        form.addRow("Wavelength (µm)", self.mode_wavelength_spin)

        self.mode_core_width_spin = QDoubleSpinBox()
        self.mode_core_width_spin.setDecimals(3)
        self.mode_core_width_spin.setRange(0.05, 50.0)
        self.mode_core_width_spin.setValue(0.5)
        form.addRow("Core width (µm)", self.mode_core_width_spin)

        self.mode_num_modes_spin = QSpinBox()
        self.mode_num_modes_spin.setRange(1, 6)
        self.mode_num_modes_spin.setValue(1)
        form.addRow("Number of modes", self.mode_num_modes_spin)

        layout.addLayout(form)

        self.mode_solve_button = QPushButton("Solve")
        self.mode_solve_button.clicked.connect(self._on_solve_mode_clicked)
        layout.addWidget(self.mode_solve_button)

        self.mode_status_label = QLabel("")
        self.mode_status_label.setWordWrap(True)
        layout.addWidget(self.mode_status_label)

        self.mode_figure = Figure(figsize=(4, 3), facecolor="#141414")
        self.mode_canvas = FigureCanvasQTAgg(self.mode_figure)
        self.mode_canvas.setMinimumHeight(280)
        self.mode_canvas.setStyleSheet("background: #141414;")
        self.mode_ax = self.mode_figure.add_subplot(111)
        self.mode_ax.set_facecolor("#141414")
        layout.addWidget(self.mode_canvas)

        return widget

    def _on_solve_mode_clicked(self) -> None:
        params = ModeProfileParams(
            wavelength_um=self.mode_wavelength_spin.value(),
            core_width_um=self.mode_core_width_spin.value(),
            num_modes=self.mode_num_modes_spin.value(),
        )
        self.mode_solve_button.setEnabled(False)
        self.mode_status_label.setText("Solving…")

        self._mode_thread = QThread(self)
        self._mode_worker = ModeWorker(self.document.project_settings, params)
        self._mode_worker.moveToThread(self._mode_thread)
        self._mode_thread.started.connect(self._mode_worker.run)
        self._mode_worker.finished.connect(self._on_mode_finished)
        self._mode_worker.failed.connect(self._on_mode_failed)
        self._mode_worker.finished.connect(self._mode_thread.quit)
        self._mode_worker.failed.connect(self._mode_thread.quit)
        self._mode_thread.start()

    def _on_mode_finished(self, result, elapsed: float) -> None:
        from matplotlib.patches import Rectangle

        self.mode_solve_button.setEnabled(True)
        check = mode_confinement(result)
        self.mode_status_label.setText(f"n_eff = {result.n_eff[0]:.4f}   ({elapsed:.2f}s)\n{check.message}")

        # abs() to match the "|psi|" title -- the raw eigenvector's sign is
        # arbitrary (an artifact of the solver, not physically meaningful),
        # so plotting it unsigned avoids a confusing two-lobed plot with a
        # washed-out background. Caught by actually looking at a rendered
        # screenshot, not just checking the data shape.
        psi = abs(result.psi[0])
        self.mode_ax.clear()
        extent = [result.y[0] * 1e6, result.y[-1] * 1e6, result.z[0] * 1e6, result.z[-1] * 1e6]
        self.mode_ax.imshow(psi.T, origin="lower", extent=extent, cmap="viridis", aspect="auto")
        core_width_um = self.mode_core_width_spin.value()
        core_thickness_um = self.document.project_settings.thickness_um
        self.mode_ax.add_patch(
            Rectangle(
                (-core_width_um / 2, -core_thickness_um / 2),
                core_width_um,
                core_thickness_um,
                fill=False,
                edgecolor="white",
                linewidth=1.5,
            )
        )
        self.mode_ax.set_xlabel("y (µm)")
        self.mode_ax.set_ylabel("z (µm)")
        self.mode_ax.set_title("|ψ| — mode profile")
        self._dark_axes(self.mode_figure, self.mode_ax)
        self.mode_canvas.draw()

    def _on_mode_failed(self, message: str) -> None:
        self.mode_solve_button.setEnabled(True)
        self.mode_status_label.setText(f"Error: {message}")

    # -- propagation tab -----------------------------------------------------

    def _build_propagation_tab(self) -> QWidget:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        widget = QWidget()
        layout = QVBoxLayout(widget)

        disclaimer = QLabel(DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(disclaimer)

        form = QFormLayout()
        self.run_wavelength_spin = QDoubleSpinBox()
        self.run_wavelength_spin.setDecimals(3)
        self.run_wavelength_spin.setRange(0.1, 10.0)
        self.run_wavelength_spin.setValue(self.document.project_settings.wavelength_um)
        form.addRow("Wavelength (µm)", self.run_wavelength_spin)

        default_params = FdtdParams(wavelength_um=self.run_wavelength_spin.value())
        self.run_cell_size_spin = QDoubleSpinBox()
        self.run_cell_size_spin.setDecimals(3)
        self.run_cell_size_spin.setRange(0.005, 1.0)
        self.run_cell_size_spin.setValue(default_params.resolved_cell_size_um())
        form.addRow("Cell size (µm)", self.run_cell_size_spin)

        self.run_time_spin = QDoubleSpinBox()
        self.run_time_spin.setDecimals(1)
        self.run_time_spin.setRange(1.0, 100000.0)
        self.run_time_spin.setValue(default_params.resolved_run_time_fs())
        form.addRow("Run time (fs)", self.run_time_spin)

        self.run_clad_thickness_label = QLabel(f"{self.document.project_settings.clad_thickness_um:.3f} µm (Project Settings)")
        form.addRow("Cladding thickness", self.run_clad_thickness_label)

        layout.addLayout(form)

        self.place_source_button = QPushButton("Place Source on Canvas")
        self.place_source_button.setCheckable(True)
        self.place_source_button.toggled.connect(self._on_place_source_toggled)
        layout.addWidget(self.place_source_button)

        self.source_table = QTableWidget(0, len(_TABLE_COLUMNS))
        self.source_table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        self.source_table.itemChanged.connect(self._on_source_table_item_changed)
        layout.addWidget(self.source_table)

        self.run_button = QPushButton("Run Simulation")
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)

        self.run_status_label = QLabel("")
        self.run_status_label.setWordWrap(True)
        layout.addWidget(self.run_status_label)

        playback_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.toggled.connect(self._on_play_toggled)
        playback_row.addWidget(self.play_button)

        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setEnabled(False)
        self.frame_slider.valueChanged.connect(self._on_slider_changed)
        playback_row.addWidget(self.frame_slider)
        layout.addLayout(playback_row)

        self.run_figure = Figure(figsize=(4, 3), facecolor="#141414")
        self.run_canvas = FigureCanvasQTAgg(self.run_figure)
        self.run_canvas.setMinimumHeight(280)
        self.run_canvas.setStyleSheet("background: #141414;")
        self.run_ax = self.run_figure.add_subplot(111)
        self.run_ax.set_facecolor("#141414")
        layout.addWidget(self.run_canvas)

        return widget

    # -- source placement ---------------------------------------------------

    def _on_place_source_toggled(self, checked: bool) -> None:
        self.view.set_source_mode(checked)

    def _on_source_placement_requested(self, x: float, y: float) -> None:
        marker = self.view.add_source_marker(x, y)
        row = self.source_table.rowCount()
        self.source_table.insertRow(row)
        self.source_table.setItem(row, _COL_X, QTableWidgetItem(f"{x:.4f}"))
        self.source_table.setItem(row, _COL_Y, QTableWidgetItem(f"{y:.4f}"))

        kind_combo = QComboBox()
        kind_combo.addItems(["dipole", "single_photon", "scripted"])
        self.source_table.setCellWidget(row, _COL_KIND, kind_combo)

        wavelength_um = self.run_wavelength_spin.value()
        self.source_table.setItem(row, _COL_WAVELENGTH, QTableWidgetItem(f"{wavelength_um:.4f}"))
        self.source_table.setItem(
            row, _COL_ENERGY, QTableWidgetItem(f"{photon_energy_ev_from_wavelength_um(wavelength_um):.4f}")
        )
        self.source_table.setItem(row, _COL_PHOTON_COUNT, QTableWidgetItem("1"))
        self.source_table.setItem(row, _COL_CORE_WIDTH, QTableWidgetItem("0.5"))
        self.source_table.setItem(row, _COL_SCRIPT, QTableWidgetItem(""))

        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(lambda checked=False, m=marker: self._on_remove_source_row(m))
        self.source_table.setCellWidget(row, _COL_REMOVE, remove_button)

        self._source_rows.append({"marker": marker})

    def _on_remove_source_row(self, marker) -> None:
        for row_idx, row_data in enumerate(self._source_rows):
            if row_data["marker"] is marker:
                self.view.remove_source_marker(marker)
                self.source_table.removeRow(row_idx)
                del self._source_rows[row_idx]
                return

    def _on_source_table_item_changed(self, item: QTableWidgetItem) -> None:
        """Wavelength and Energy are two views of the same underlying
        quantity — editing either updates the other, using the conversion
        helpers (also used/tested independently in fdtd_sim.py)."""
        if self._syncing_wavelength_energy:
            return
        column = item.column()
        if column not in (_COL_WAVELENGTH, _COL_ENERGY):
            return
        try:
            value = float(item.text())
        except ValueError:
            return

        self._syncing_wavelength_energy = True
        try:
            row = item.row()
            if column == _COL_WAVELENGTH:
                energy_ev = photon_energy_ev_from_wavelength_um(value)
                other = self.source_table.item(row, _COL_ENERGY)
                if other is not None:
                    other.setText(f"{energy_ev:.4f}")
            else:
                wavelength_um = wavelength_um_from_photon_energy_ev(value)
                other = self.source_table.item(row, _COL_WAVELENGTH)
                if other is not None:
                    other.setText(f"{wavelength_um:.4f}")
        except ValueError:
            pass  # non-positive value mid-edit; leave the other column alone
        finally:
            self._syncing_wavelength_energy = False

    def _collect_source_specs(self) -> tuple[SourceSpec, ...]:
        specs = []
        for row in range(self.source_table.rowCount()):
            x_um = float(self.source_table.item(row, _COL_X).text())
            y_um = float(self.source_table.item(row, _COL_Y).text())
            kind = self.source_table.cellWidget(row, _COL_KIND).currentText()
            wavelength_um = float(self.source_table.item(row, _COL_WAVELENGTH).text())
            photon_count = int(self.source_table.item(row, _COL_PHOTON_COUNT).text())
            core_width_um = float(self.source_table.item(row, _COL_CORE_WIDTH).text()) if kind == "single_photon" else None
            script = self.source_table.item(row, _COL_SCRIPT).text() if kind == "scripted" else None
            specs.append(
                SourceSpec(
                    x_um=x_um,
                    y_um=y_um,
                    kind=kind,
                    wavelength_um=wavelength_um,
                    photon_count=photon_count,
                    core_width_um=core_width_um,
                    script=script,
                )
            )
        return tuple(specs)

    # -- running the simulation ----------------------------------------------

    def _current_params(self) -> FdtdParams:
        return FdtdParams(
            wavelength_um=self.run_wavelength_spin.value(),
            cell_size_um=self.run_cell_size_spin.value(),
            run_time_fs=self.run_time_spin.value(),
            sources=self._collect_source_specs(),
        )

    def _on_run_clicked(self) -> None:
        params = self._current_params()
        self._last_params = params
        try:
            cell_count = estimate_grid_cell_count(self.document, params)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot run simulation", str(exc))
            return

        # n_steps isn't known without building the Simulation; estimate it
        # the same way Simulation does (run_time / dt), cheaply, from the
        # resolved params alone, to avoid a second expensive build.
        cell_size_m = params.resolved_cell_size_um() * 1e-6
        courant = 0.99
        dt = courant / (299792458.0 * (3 ** 0.5) / cell_size_m)
        n_steps = int(params.resolved_run_time_fs() * 1e-15 / dt) + 1
        estimated_seconds = estimate_run_seconds((cell_count, 1, 1), n_steps)

        if estimated_seconds > _RUN_TIME_WARNING_SECONDS:
            reply = QMessageBox.question(
                self,
                "Large simulation",
                f"This grid has about {cell_count:,} cells and is estimated to take "
                f"roughly {estimated_seconds:.0f}s (NumPy backend, no GPU — depends on "
                "your machine). Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        self.run_status_label.setText("Running…")
        self._fdtd_thread = QThread(self)
        self._fdtd_worker = FdtdWorker(self.document, params)
        self._fdtd_worker.moveToThread(self._fdtd_thread)
        self._fdtd_thread.started.connect(self._fdtd_worker.run)
        self._fdtd_worker.finished.connect(self._on_fdtd_finished)
        self._fdtd_worker.failed.connect(self._on_fdtd_failed)
        self._fdtd_worker.finished.connect(self._fdtd_thread.quit)
        self._fdtd_worker.failed.connect(self._fdtd_thread.quit)
        self._fdtd_thread.start()

    def _on_fdtd_finished(self, sim, result, elapsed: float) -> None:
        self.run_button.setEnabled(True)
        self._last_sim = sim
        self._last_result = result
        self._chip_outline_drawn = False

        arr = result.fields["field"]["Ez"]
        n_frames = arr.shape[0]
        self.run_status_label.setText(f"Done in {elapsed:.2f}s — {n_frames} frames, grid {arr.shape[1]}×{arr.shape[2]}×{arr.shape[3]}")

        self.frame_slider.setEnabled(n_frames > 1)
        self.frame_slider.setRange(0, max(n_frames - 1, 0))
        self.play_button.setEnabled(n_frames > 1)
        self.frame_slider.setValue(0)
        self._draw_frame(0)

    def _on_fdtd_failed(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.run_status_label.setText(f"Error: {message}")

    # -- movie playback -------------------------------------------------------

    def _on_slider_changed(self, value: int) -> None:
        self._draw_frame(value)

    def _on_play_toggled(self, checked: bool) -> None:
        if checked:
            self._play_timer.start()
            self.play_button.setText("Pause")
        else:
            self._play_timer.stop()
            self.play_button.setText("Play")

    def _advance_frame(self) -> None:
        n_frames = self.frame_slider.maximum() + 1
        next_value = (self.frame_slider.value() + 1) % max(n_frames, 1)
        self.frame_slider.setValue(next_value)

    def _draw_frame(self, frame_index: int) -> None:
        if self._last_result is None or self._last_sim is None:
            return
        arr = self._last_result.fields["field"]["Ez"]
        z_idx = nearest_z_index(self._last_sim.grid, 0.0)
        frame = arr[frame_index, :, :, z_idx]

        x_coords = self._last_sim.grid.coords[0] * 1e6
        y_coords = self._last_sim.grid.coords[1] * 1e6
        extent = [x_coords[0], x_coords[-1], y_coords[0], y_coords[-1]]

        vmax = float(max(abs(frame.min()), abs(frame.max()), 1e-30))

        if not self._chip_outline_drawn:
            self.run_ax.clear()
            self._field_im = self.run_ax.imshow(
                frame.T, origin="lower", extent=extent, cmap="RdBu", alpha=0.75, aspect="equal", vmin=-vmax, vmax=vmax
            )
            # Drawn after the field image (not before — fixing a real bug
            # found by actually screenshotting this: setting axis limits
            # to the chip's own bbox *before* the field image was added
            # clipped almost the whole field out of view, since the
            # simulated domain is wider than the bbox once PML/padding is
            # included. The outline + xlim/ylim below now match the field's
            # own extent instead, so the full simulated domain stays
            # visible with the chip geometry drawn as a reference on top.
            self._draw_chip_outline(self.run_ax)
            self._draw_source_markers(self.run_ax)
            self.run_ax.set_xlim(extent[0], extent[1])
            self.run_ax.set_ylim(extent[2], extent[3])
            self.run_ax.set_xlabel("x (µm)")
            self.run_ax.set_ylabel("y (µm)")
            self.run_ax.set_title("Ez field, top-down (mid-core height)")
            self._dark_axes(self.run_figure, self.run_ax)
            self._chip_outline_drawn = True
        else:
            self._field_im.set_data(frame.T)
            self._field_im.set_clim(-vmax, vmax)
        self.run_canvas.draw()

    @staticmethod
    def _dark_axes(fig, ax) -> None:
        bg = "#141414"
        fig.patch.set_facecolor(bg)
        ax.set_facecolor(bg)
        for item in (ax.xaxis.label, ax.yaxis.label):
            item.set_color("#bbbbbb")
        ax.title.set_color("#eeeeee")
        ax.tick_params(colors="#777777", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#2e2e2e")

    def _draw_source_markers(self, ax) -> None:
        if self._last_params is None:
            return
        for i, src in enumerate(self._last_params.sources, 1):
            ax.plot(
                src.x_um, src.y_um,
                marker="*", markersize=12,
                color="#ffaa00", markeredgecolor="#000000",
                markeredgewidth=0.4, linestyle="none", zorder=10,
            )
            ax.annotate(
                f"S{i}", (src.x_um, src.y_um),
                xytext=(5, 4), textcoords="offset points",
                color="#ffaa00", fontsize=7.5, fontweight="bold",
                zorder=11,
            )

    def _draw_chip_outline(self, ax) -> None:
        from matplotlib.patches import Polygon as MplPolygon

        shapes = shapes_for_cell(self.document.top)
        for shapes_list in shapes.values():
            for hull, _holes in shapes_list:
                ax.add_patch(MplPolygon(hull, closed=True, fill=False, edgecolor="black", linewidth=0.8))

    # -- lifecycle ------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._play_timer.stop()
        self.view.set_source_mode(False)
        self.view.clear_source_markers()
        super().closeEvent(event)
