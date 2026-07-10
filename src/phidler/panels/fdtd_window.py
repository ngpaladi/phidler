"""FDTD simulation window — native Qt rendering, no matplotlib dependency."""
from __future__ import annotations

import dataclasses
import math
import time

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from phidler.canvas.view import C0_UM_PER_FS, C0_UM_PER_NS, UNIT_MODES, LayoutView, nice_ticks
from phidler.fdtd_sim import (
    DISCLAIMER,
    FdtdParams,
    ModeProfileParams,
    SimulationConfig,
    SourceSpec,
    build_mode_solver,
    build_simulation,
    check_run_feasible,
    estimate_grid_cell_count,
    estimate_memory_gb,
    estimate_run_seconds,
    feasible_cell_budget,
    gpu_available,
    gpu_backend_name,
    jax_available,
    limit_solver_threads,
    mode_confinement,
    nearest_z_index,
    numba_available,
    photon_energy_ev_from_wavelength_um,
    run_simulation,
    solve_mode_profile,
    suggest_region_um,
    wavelength_um_from_photon_energy_ev,
)
from phidler.model.document import LayoutDocument, shapes_for_cell

_RUN_TIME_WARNING_SECONDS = 5.0
_MEMORY_WARNING_GB = 4.0  # warn before a run whose solve working set is this large
_REGION_MARGIN_UM = 2.0  # breathing room added around a "selection only" region
# Upper bound on the auto-suggested region when a layout is too big to run whole:
# picked so the region both fits memory and finishes in a reasonable time (a
# biggest-that-fits region would run but be painfully slow). ~a 120 µm window.
_SUGGESTED_MAX_CELLS = 40_000_000

_TABLE_COLUMNS = [
    "X (µm)",
    "Y (µm)",
    "Kind",
    "Wavelength (µm)",
    "Energy (eV)",
    "Photon count",
    "Core width (µm)",
    "Script (kind=scripted)",
    "β = v/c (cherenkov)",
    "Tilt ° from vertical (cherenkov)",
    "Track µm in z (cherenkov)",
    "",
]
(
    _COL_X,
    _COL_Y,
    _COL_KIND,
    _COL_WAVELENGTH,
    _COL_ENERGY,
    _COL_PHOTON_COUNT,
    _COL_CORE_WIDTH,
    _COL_SCRIPT,
    _COL_BETA,
    _COL_TRACK_DIR,
    _COL_TRACK_LEN,
    _COL_REMOVE,
) = range(12)

# Which parameter cells actually feed each source kind (mirrors how build_source
# in fdtd_sim.py consumes a SourceSpec). Cells outside a row's set are greyed and
# made non-editable so only the parameters that matter for the chosen kind read
# as live. X/Y/Kind/Remove are always active, so they're omitted here.
_RELEVANT_COLUMNS: dict[str, set[int]] = {
    "dipole": {_COL_WAVELENGTH, _COL_ENERGY},
    "single_photon": {_COL_WAVELENGTH, _COL_ENERGY, _COL_PHOTON_COUNT, _COL_CORE_WIDTH},
    "scripted": {_COL_SCRIPT},
    "cherenkov": {_COL_WAVELENGTH, _COL_ENERGY, _COL_BETA, _COL_TRACK_DIR, _COL_TRACK_LEN},
}
# Every togglable parameter column (the always-on X/Y/Kind/Remove excluded).
_PARAM_COLUMNS = (
    _COL_WAVELENGTH, _COL_ENERGY, _COL_PHOTON_COUNT, _COL_CORE_WIDTH,
    _COL_SCRIPT, _COL_BETA, _COL_TRACK_DIR, _COL_TRACK_LEN,
)

# ---------------------------------------------------------------------------
# Colormaps: 256-entry uint8 (N, 3) tables built by piecewise-linear interp.
# ---------------------------------------------------------------------------

def _build_cmap(control_pts: list[tuple[float, float, float, float]]) -> np.ndarray:
    cp = np.array(control_pts, dtype=np.float32)
    ts, rgb = cp[:, 0], cp[:, 1:]
    idx = np.linspace(0.0, 1.0, 256)
    table = np.zeros((256, 3), dtype=np.uint8)
    for ch in range(3):
        table[:, ch] = np.clip(np.interp(idx, ts, rgb[:, ch]) * 255, 0, 255).astype(np.uint8)
    return table


_VIRIDIS = _build_cmap([
    (0.000, 0.267, 0.005, 0.329),
    (0.250, 0.282, 0.300, 0.529),
    (0.500, 0.129, 0.567, 0.550),
    (0.750, 0.369, 0.718, 0.388),
    (1.000, 0.993, 0.906, 0.144),
])

_RDBU = _build_cmap([
    (0.000, 0.843, 0.188, 0.153),
    (0.250, 0.957, 0.647, 0.510),
    (0.500, 1.000, 1.000, 1.000),
    (0.750, 0.573, 0.773, 0.871),
    (1.000, 0.263, 0.576, 0.765),
])

# Circuit-element outline color, drawn over the field in both FDTD views. Cyan
# reads clearly against both the viridis mode profile and the red/white/blue
# diverging propagation field (and matches the design canvas's routing accent).
_OUTLINE_COLOR = "#00e0ff"

# ---------------------------------------------------------------------------
# Cladding materials dropdown
# ---------------------------------------------------------------------------

_CLADDING_MATERIALS: list[tuple[str, float | None]] = [
    ("SiO₂ — thermal oxide (n = 1.444)", 1.444),
    ("SiO₂ — PECVD (n = 1.460)", 1.460),
    ("Air (n = 1.000)", 1.000),
    ("Si₃N₄ (n = 2.000)", 2.000),
    ("BCB (n = 1.535)", 1.535),
    ("SU-8 (n = 1.580)", 1.580),
    ("Water (n = 1.330)", 1.330),
    ("Custom…", None),
]

# ---------------------------------------------------------------------------
# Run-time slider: log scale 10–1000 fs over 200 integer steps
# ---------------------------------------------------------------------------

_RT_MIN_FS = 10.0
_RT_MAX_FS = 100_000.0  # slider reaches 100 ps; the spin box goes higher still
_RT_STEPS = 200

# Cap on recorded movie frames: a long run has hundreds of thousands of
# timesteps, and recording every few would store (and play back) an unusable
# number of frames — so the monitor interval is stretched to keep frames near
# this count regardless of run length.
_MAX_MOVIE_FRAMES = 300
_BASE_PLAY_INTERVAL_MS = 100  # frame period at 1× playback speed (10 fps)


def _slider_to_fs(v: int) -> float:
    t = v / _RT_STEPS
    return _RT_MIN_FS * (_RT_MAX_FS / _RT_MIN_FS) ** t


def _fs_to_slider(fs: float) -> int:
    fs_c = max(_RT_MIN_FS, min(_RT_MAX_FS, fs))
    t = math.log(fs_c / _RT_MIN_FS) / math.log(_RT_MAX_FS / _RT_MIN_FS)
    return int(round(t * _RT_STEPS))


# ---------------------------------------------------------------------------
# Coordinate units — aliases for the canonical names from canvas.view
# ---------------------------------------------------------------------------

_C0_UM_PER_FS = C0_UM_PER_FS
_C0_UM_PER_NS = C0_UM_PER_NS
_UNIT_MODES = UNIT_MODES
_nice_ticks = nice_ticks


# ---------------------------------------------------------------------------
# QImage helper
# ---------------------------------------------------------------------------

def _array_to_qimage(
    data: np.ndarray,
    cmap_table: np.ndarray,
    symmetric: bool = False,
) -> QImage:
    """Convert a 2-D float array to a QImage suitable for a Y-flipped scene.

    data shape: (Nx, Ny) where Nx is the scene-x (column) dimension and Ny
    is the scene-y (row) dimension.  Row 0 in the returned image corresponds
    to the minimum scene-y value so that, with the view's scale(1, -1) Y-flip,
    minimum-y data appears at the bottom of the viewport (correct orientation).
    """
    if symmetric:
        vmax = max(float(np.abs(data).max()), 1e-30)
        t = ((data + vmax) / (2.0 * vmax) * 255.0).clip(0, 255).astype(np.uint8)
    else:
        lo, hi = float(data.min()), float(data.max())
        if hi == lo:
            hi = lo + 1.0
        t = ((data - lo) / (hi - lo) * 255.0).clip(0, 255).astype(np.uint8)

    # t.T → (Ny, Nx): row 0 = ymin data, matching Y-flipped view orientation
    rgb = cmap_table[t.T]                         # (Ny, Nx, 3)
    h, w = rgb.shape[:2]
    rgb_c = np.ascontiguousarray(rgb)
    img = QImage(rgb_c.data, w, h, w * 3, QImage.Format_RGB888)
    return img.copy()                             # .copy() owns the pixel memory


# ---------------------------------------------------------------------------
# FieldView — QGraphicsView with Y-flip, pan, zoom, and field-image display
# ---------------------------------------------------------------------------

class FieldView(QGraphicsView):
    """Renders a 2-D field result as a QGraphicsPixmapItem in scene (µm)
    coordinates, sharing the same Y-flipped coordinate system as LayoutView
    so that 'copy viewport from design canvas' is a single fitInView call."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.scale(1.0, -1.0)                     # Y-up, same as LayoutView
        self.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.setMinimumHeight(240)

        self._pix_item = None                     # QGraphicsPixmapItem | None
        self._overlay_items: list = []
        self._unit_mode: str = "um"               # "um" | "nm" | "fs" | "ns"
        self._n_eff: float = 1.0                  # phase index for µm→time conversion

        # Placeholder label parented to the viewport widget so it sits inside
        # the scene area rather than over the frame/scrollbars.
        self._placeholder = QLabel("No result — click Compute to run", self.viewport())
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #555555; font-size: 10pt;")
        self._placeholder.setGeometry(self.viewport().rect())

    # -- event overrides -------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._placeholder.setGeometry(self.viewport().rect())

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
        new_scale = abs(self.transform().m11()) * factor
        if 0.001 < new_scale < 50_000:
            self.scale(factor, factor)

    # -- coordinate units ------------------------------------------------------

    def set_unit_mode(self, mode: str) -> None:
        """Switch axis display units: 'um', 'nm', 'fs', or 'ns'."""
        self._unit_mode = mode
        self.viewport().update()

    def set_n_eff(self, n: float) -> None:
        """Set the effective phase index used for µm→time propagation-time conversion."""
        self._n_eff = max(n, 1e-6)
        if self._unit_mode in ("fs", "ns"):
            self.viewport().update()

    def _to_display(self, um: float) -> float:
        """Convert a scene µm coordinate to the current display unit.

        Time modes use t = x · n_eff / c₀ (phase propagation through the core).
        """
        if self._unit_mode == "nm":
            return um * 1000.0
        if self._unit_mode == "fs":
            return um * self._n_eff / _C0_UM_PER_FS
        if self._unit_mode == "ns":
            return um * self._n_eff / _C0_UM_PER_NS
        return um

    def _unit_str(self) -> str:
        if self._unit_mode == "nm":
            return "nm"
        if self._unit_mode == "fs":
            return f"fs  (n = {self._n_eff:.3f})"
        if self._unit_mode == "ns":
            return f"ns  (n = {self._n_eff:.3f})"
        return "µm"

    def _to_scene(self, display_val: float) -> float:
        """Inverse of _to_display: display unit value → scene µm."""
        if self._unit_mode == "nm":
            return display_val / 1000.0
        if self._unit_mode == "fs":
            return display_val * _C0_UM_PER_FS / self._n_eff
        if self._unit_mode == "ns":
            return display_val * _C0_UM_PER_NS / self._n_eff
        return display_val

    # -- grid + axis labels ----------------------------------------------------

    def drawBackground(self, painter, rect) -> None:
        super().drawBackground(painter, rect)
        if self._pix_item is None:
            return

        # rect is the exposed scene rectangle (scene Y increases upward).
        # Compute nice tick positions in display units, then convert to scene µm.
        xmin, xmax = rect.left(), rect.right()
        ymin, ymax = rect.top(), rect.bottom()   # ymin < ymax in scene coords

        x_disp = _nice_ticks(self._to_display(xmin), self._to_display(xmax))
        y_disp = _nice_ticks(self._to_display(ymin), self._to_display(ymax))
        x_scene = [self._to_scene(d) for d in x_disp]
        y_scene = [self._to_scene(d) for d in y_disp]

        pen = QPen(QColor("#383838"), 0)          # cosmetic (1-px) dark grid
        painter.setPen(pen)
        for x in x_scene:
            painter.drawLine(QPointF(x, ymin), QPointF(x, ymax))
        for y in y_scene:
            painter.drawLine(QPointF(xmin, y), QPointF(xmax, y))

    def drawForeground(self, painter, rect) -> None:
        if self._pix_item is None:
            return

        xmin, xmax = rect.left(), rect.right()
        ymin, ymax = rect.top(), rect.bottom()

        x_disp = _nice_ticks(self._to_display(xmin), self._to_display(xmax))
        y_disp = _nice_ticks(self._to_display(ymin), self._to_display(ymax))
        x_scene = [self._to_scene(d) for d in x_disp]
        y_scene = [self._to_scene(d) for d in y_disp]

        painter.save()
        painter.resetTransform()                  # switch to viewport pixel coords

        vp = self.viewport()
        vp_w, vp_h = vp.width(), vp.height()

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        fm = painter.fontMetrics()

        label_color = QColor("#cccccc")
        shadow_color = QColor(0, 0, 0, 160)

        def draw_label(x_px: int, y_px: int, text: str) -> None:
            painter.setPen(QPen(shadow_color))
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                painter.drawText(x_px + dx, y_px + dy, text)
            painter.setPen(QPen(label_color))
            painter.drawText(x_px, y_px, text)

        margin_bottom = 16
        margin_left = 40

        # X-axis tick labels near the bottom of the viewport
        for x_s, x_d in zip(x_scene, x_disp):
            px = int(self.mapFromScene(QPointF(x_s, 0)).x())
            if not (margin_left <= px <= vp_w - 5):
                continue
            label = f"{x_d:.4g}"
            tw = fm.horizontalAdvance(label)
            draw_label(px - tw // 2, vp_h - 4, label)

        # Y-axis tick labels near the left of the viewport
        for y_s, y_d in zip(y_scene, y_disp):
            py = int(self.mapFromScene(QPointF(0, y_s)).y())
            if not (5 <= py <= vp_h - margin_bottom):
                continue
            draw_label(4, py + fm.ascent() // 2, f"{y_d:.4g}")

        # Axis unit label
        unit = self._unit_str()
        painter.setPen(QPen(QColor("#888888")))
        painter.drawText(vp_w // 2 - fm.horizontalAdvance(unit) // 2, vp_h - 2, unit)

        painter.restore()

    # -- field image -----------------------------------------------------------

    def set_image(
        self,
        data: np.ndarray,
        extent: tuple[float, float, float, float],
        cmap_table: np.ndarray,
        *,
        symmetric: bool = False,
    ) -> None:
        """Place a field image covering (xmin, xmax, ymin, ymax) in scene µm.

        data: shape (Nx, Ny), scene x = axis 0, scene y = axis 1.
        """
        img = _array_to_qimage(data, cmap_table, symmetric=symmetric)
        pixmap = QPixmap.fromImage(img)

        xmin, xmax, ymin, ymax = extent
        nx, ny = data.shape
        sx = (xmax - xmin) / nx
        sy = (ymax - ymin) / ny

        if self._pix_item is None:
            self._pix_item = self.scene().addPixmap(pixmap)
        else:
            self._pix_item.setPixmap(pixmap)

        self._pix_item.setTransform(QTransform().scale(sx, sy))
        self._pix_item.setPos(xmin, ymin)
        self._pix_item.setZValue(-10)
        self._placeholder.setVisible(False)

    def update_image(
        self,
        data: np.ndarray,
        cmap_table: np.ndarray,
        *,
        symmetric: bool = False,
    ) -> None:
        """Update pixel data only (no transform change — for frame animation)."""
        if self._pix_item is None:
            return
        img = _array_to_qimage(data, cmap_table, symmetric=symmetric)
        self._pix_item.setPixmap(QPixmap.fromImage(img))

    # -- overlays --------------------------------------------------------------

    def clear_overlays(self) -> None:
        for item in self._overlay_items:
            self.scene().removeItem(item)
        self._overlay_items.clear()

    def add_polygon_overlay(
        self,
        hull: list[tuple[float, float]],
        pen_color: str = _OUTLINE_COLOR,
    ) -> None:
        poly = QPolygonF([QPointF(x, y) for x, y in hull])
        item = self.scene().addPolygon(poly, QPen(QColor(pen_color), 0), QBrush(Qt.NoBrush))
        item.setZValue(6)  # above the field image, below source markers
        self._overlay_items.append(item)

    def add_source_marker(self, x: float, y: float) -> None:
        r = 0.25
        item = self.scene().addEllipse(
            x - r, y - r, 2 * r, 2 * r,
            QPen(QColor("#cc7700"), 0),
            QBrush(QColor("#ffaa00")),
        )
        item.setZValue(10)
        self._overlay_items.append(item)

    def add_rect_overlay(
        self, x: float, y: float, w: float, h: float, pen_color: str = _OUTLINE_COLOR
    ) -> None:
        item = self.scene().addRect(x, y, w, h, QPen(QColor(pen_color), 0), QBrush(Qt.NoBrush))
        item.setZValue(6)
        self._overlay_items.append(item)

    # -- viewport helpers ------------------------------------------------------

    def fit_to_image(self) -> None:
        if self._pix_item is not None:
            rect = self._pix_item.mapToScene(self._pix_item.boundingRect()).boundingRect()
            self.fitInView(rect, Qt.KeepAspectRatio)

    def copy_viewport_from(self, other: LayoutView) -> None:
        """Snap to the design canvas's currently visible scene region."""
        visible = other.mapToScene(other.viewport().rect()).boundingRect()
        self.fitInView(visible, Qt.KeepAspectRatio)


# ---------------------------------------------------------------------------
# _CladRow — cladding material selector widget
# ---------------------------------------------------------------------------

class _CladRow(QWidget):
    """Dropdown of preset cladding materials plus an optional custom-n spinbox."""

    index_changed = Signal(float)

    def __init__(self, default_n: float = 1.444, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._combo = QComboBox()
        for name, _ in _CLADDING_MATERIALS:
            self._combo.addItem(name)
        self._combo.setToolTip(
            "Refractive index of the material surrounding the waveguide core. Pick a "
            "preset or choose Custom… to type an index — a higher cladding index reduces "
            "the core/cladding contrast and so weakens confinement."
        )
        layout.addWidget(self._combo)

        self._custom_spin = QDoubleSpinBox()
        self._custom_spin.setRange(1.0, 5.0)
        self._custom_spin.setDecimals(3)
        self._custom_spin.setSingleStep(0.01)
        self._custom_spin.setValue(default_n)
        self._custom_spin.setVisible(False)
        layout.addWidget(self._custom_spin)

        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._custom_spin.valueChanged.connect(self._on_custom_changed)

        # Select the entry whose n is closest to default_n
        best_idx = 0
        best_err = float("inf")
        for i, (_, n) in enumerate(_CLADDING_MATERIALS):
            if n is not None and abs(n - default_n) < best_err:
                best_err = abs(n - default_n)
                best_idx = i
        self._combo.setCurrentIndex(best_idx)

    def clad_index(self) -> float:
        _, n = _CLADDING_MATERIALS[self._combo.currentIndex()]
        return self._custom_spin.value() if n is None else n

    def set_clad_index(self, n: float) -> None:
        """Restore a saved cladding index: select the preset that matches it,
        else fall back to the Custom… entry with n typed into the spinbox."""
        for i, (_, preset) in enumerate(_CLADDING_MATERIALS):
            if preset is not None and math.isclose(preset, n, abs_tol=1e-6):
                self._combo.setCurrentIndex(i)
                return
        custom_idx = next(i for i, (_, p) in enumerate(_CLADDING_MATERIALS) if p is None)
        self._custom_spin.setValue(n)  # set before switching so the row shows the right value
        self._combo.setCurrentIndex(custom_idx)

    def _on_combo_changed(self, idx: int) -> None:
        _, n = _CLADDING_MATERIALS[idx]
        self._custom_spin.setVisible(n is None)
        self.index_changed.emit(self.clad_index())

    def _on_custom_changed(self, value: float) -> None:
        self.index_changed.emit(value)


# ---------------------------------------------------------------------------
# Worker objects (off-thread compute, same pattern as before)
# ---------------------------------------------------------------------------

class ModeWorker(QObject):
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
    finished = Signal(object, object, float)  # Simulation, Result, elapsed_seconds
    failed = Signal(str)
    progress = Signal(int, int)  # (step, n_steps) — emitted from the worker thread

    def __init__(self, document: LayoutDocument, params: FdtdParams, region_um=None,
                 remote=False, remote_cfg=None) -> None:
        super().__init__()
        self.document = document
        self.params = params
        self.region_um = region_um
        self.remote = remote
        self.remote_cfg = remote_cfg

    def run(self) -> None:
        # progress.emit is safe to call from this worker thread: Qt queues the
        # cross-thread delivery to the GUI thread. All three backends report
        # through the same signal — the in-process solver via a callback, the
        # subprocess/remote paths by parsing markers streamed back.
        try:
            if self.remote:
                # Ship the job to the configured SSH host, run it there, bring
                # the result back. The worker thread blocks on ssh while the UI
                # stays live — same shape as the GPU subprocess path below.
                # Checked before use_gpu so a remote-GPU run doesn't take the
                # local subprocess branch.
                from phidler.fdtd_remote import run_on_remote

                sim, result, elapsed = run_on_remote(
                    self.document, self.params, self.region_um, self.remote_cfg,
                    progress_callback=self.progress.emit,
                )
            elif self.params.use_gpu and not self.params.out_of_core:
                # The GPU (CuPy) backend can't tear down in a worker thread
                # without crashing, and freezes the UI on the main thread — so
                # run it in a child process and just wait on it here. CuPy lives
                # in the child (clean teardown), this thread only blocks on the
                # subprocess, so the UI stays responsive. (out_of_core is
                # NumPy-only, so it falls through to the in-process branch.)
                from phidler.fdtd_subprocess import run_in_subprocess

                sim, result, elapsed = run_in_subprocess(
                    self.document, self.params, self.region_um,
                    progress_callback=self.progress.emit,
                )
            else:
                # In-process CPU/numba solve (also the out-of-core path): it
                # shares this (GUI) process, so cap numba's parallel kernel to
                # leave the desktop some cores — otherwise a long run pins every
                # core and freezes the whole machine (mouse still moves) until it
                # finishes. renice=False: this thread is discarded after the run,
                # but lowering the GUI process's own priority would slow the UI,
                # so only cap threads. Out-of-core keeps peak RAM bounded to a
                # tile, so a grid too big for RAM can still run here.
                limit_solver_threads(renice=False)
                t0 = time.time()
                sim = build_simulation(self.document, self.params, region_um=self.region_um)
                sim.progress_callback = self.progress.emit
                result = run_simulation(sim, out_of_core=self.params.out_of_core)
                elapsed = time.time() - t0
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(sim, result, elapsed)


class _RemoteOpWorker(QObject):
    """Runs a remote connectivity check or deploy off the GUI thread, forwarding
    each output line as it arrives (so the deploy's pip/build output streams live
    into the dialog) and a final ok/done signal."""

    line = Signal(str)
    done = Signal(bool)  # success

    def __init__(self, cfg, op: str) -> None:
        super().__init__()
        self.cfg = cfg
        self.op = op  # "check" | "deploy"

    def run(self) -> None:
        from phidler.fdtd_remote import check_remote, deploy_to_remote

        try:
            if self.op == "check":
                ok, msg = check_remote(self.cfg)
                self.line.emit(msg)
            else:
                ok = deploy_to_remote(self.cfg, self.line.emit)
        except Exception as exc:  # never let the worker die silently
            self.line.emit(f"Error: {exc}")
            ok = False
        self.done.emit(bool(ok))


class RemoteConfigDialog(QDialog):
    """Set up and test offloading FDTD runs to a remote SSH host. Captures the
    host alias, the remote directory + Python interpreter to install into, and
    whether to request the remote GPU; persists them via remote_config. The
    'Test connection' and 'Set up remote' buttons run off the GUI thread and
    stream their output into the log pane so the window stays responsive."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remote Simulation Server")
        self.resize(620, 480)
        self._op_thread: QThread | None = None
        self._op_worker: _RemoteOpWorker | None = None

        from phidler.remote_config import DEFAULT_REMOTE_DIR, load_remote_config

        cfg = load_remote_config()

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.alias_edit = QLineEdit(cfg.alias)
        self.alias_edit.setPlaceholderText("host alias from ~/.ssh/config, e.g. gpubox")
        self.alias_edit.setToolTip(
            "A Host entry from your ~/.ssh/config. This is all you need — phidler "
            "installs under a default directory and creates its own venv there on "
            "setup. It runs `ssh <host> …` and lets your SSH config and agent/keys "
            "handle authentication (no passwords stored; key-based auth required)."
        )
        form.addRow("SSH host", self.alias_edit)

        self.use_gpu_check = QCheckBox("Use GPU on the remote host")
        self.use_gpu_check.setChecked(cfg.use_gpu)
        self.use_gpu_check.setToolTip(
            "Request photonfdtd's GPU (CuPy) backend on the remote — independent of "
            "whether this machine has a GPU. If the remote has no GPU it falls back to "
            "CPU, and the result reports the backend that actually ran."
        )
        form.addRow("Acceleration", self.use_gpu_check)
        layout.addLayout(form)

        # Everything below is optional — a bare host uses these derived defaults.
        # The group starts collapsed (unchecked) unless the user already set one.
        self.advanced_group = QGroupBox("Advanced (optional) — override install location")
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(
            bool(cfg.remote_dir or cfg.remote_python or cfg.local_photonfdtd_dir)
        )
        adv_form = QFormLayout(self.advanced_group)

        self.remote_dir_edit = QLineEdit(cfg.remote_dir)
        self.remote_dir_edit.setPlaceholderText(f"default: {DEFAULT_REMOTE_DIR}")
        self.remote_dir_edit.setToolTip(
            "Directory on the remote where phidler's and photonfdtd's source are "
            f"uploaded and installed (created if missing). Blank uses {DEFAULT_REMOTE_DIR}."
        )
        adv_form.addRow("Remote directory", self.remote_dir_edit)

        self.remote_python_edit = QLineEdit(cfg.remote_python)
        self.remote_python_edit.setPlaceholderText("default: <remote directory>/.venv/bin/python (created on setup)")
        self.remote_python_edit.setToolTip(
            "The remote Python phidler is installed into and runs from. Blank means a "
            "venv under the remote directory that 'Set up remote' creates for you; set "
            "an explicit interpreter only if you want to install into an existing env."
        )
        adv_form.addRow("Remote Python", self.remote_python_edit)

        self.local_pf_edit = QLineEdit(cfg.local_photonfdtd_dir)
        self.local_pf_edit.setPlaceholderText("(optional) blank installs photonfdtd from GitHub")
        self.local_pf_edit.setToolTip(
            "Where a local photonfdtd source checkout lives, to upload and install "
            "during setup. Leave blank to install photonfdtd from its public GitHub "
            "repo instead — the usual case, since you needn't have it locally."
        )
        adv_form.addRow("Local photonfdtd", self.local_pf_edit)

        layout.addWidget(self.advanced_group)

        button_row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._on_test)
        button_row.addWidget(self.test_button)
        self.setup_button = QPushButton("Set up remote")
        self.setup_button.setToolTip(
            "One-time: upload the phidler + photonfdtd source and `pip install -e` both "
            "into the remote Python's environment. Re-run after updating either package."
        )
        self.setup_button.clicked.connect(self._on_setup)
        button_row.addWidget(self.setup_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Connection / setup output appears here.")
        layout.addWidget(self.log, stretch=1)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        self._buttons.button(QDialogButtonBox.Save).setText("Save")
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _current_config(self):
        from phidler.remote_config import RemoteConfig

        # When the Advanced group is collapsed, ignore its fields so a bare host
        # falls back to the derived defaults (managed dir + venv), even if the
        # boxes still hold previously-typed text.
        advanced = self.advanced_group.isChecked()
        return RemoteConfig(
            alias=self.alias_edit.text().strip(),
            remote_dir=self.remote_dir_edit.text().strip() if advanced else "",
            remote_python=self.remote_python_edit.text().strip() if advanced else "",
            use_gpu=self.use_gpu_check.isChecked(),
            local_photonfdtd_dir=self.local_pf_edit.text().strip() if advanced else "",
        )

    def _on_save(self) -> None:
        from phidler.remote_config import save_remote_config

        save_remote_config(self._current_config())
        self.accept()

    def _on_test(self) -> None:
        self.log.clear()
        self._start_op("check")

    def _on_setup(self) -> None:
        self.log.clear()
        self._append("Starting remote setup — this can take a while on first install.")
        self._start_op("deploy")

    def _start_op(self, op: str) -> None:
        if self._op_thread is not None:  # one operation at a time
            return
        self._set_busy(True)
        self._op_thread = QThread(self)
        self._op_worker = _RemoteOpWorker(self._current_config(), op)
        self._op_worker.moveToThread(self._op_thread)
        self._op_thread.started.connect(self._op_worker.run)
        self._op_worker.line.connect(self._append)
        self._op_worker.done.connect(self._on_op_done)
        self._op_worker.done.connect(self._op_thread.quit)
        self._op_thread.finished.connect(self._op_worker.deleteLater)
        self._op_thread.finished.connect(self._on_op_thread_finished)
        self._op_thread.start()

    def _on_op_done(self, ok: bool) -> None:
        # Runs on the GUI thread when the worker finishes. Only touch the UI
        # here — do NOT wait() on the thread: its event loop hasn't returned yet
        # (the queued quit() runs after this slot), so waiting would deadlock.
        self._append("\nDone." if ok else "\nFinished with errors.")
        self._set_busy(False)

    def _on_op_thread_finished(self) -> None:
        # The worker thread's event loop has actually exited now, so it's safe to
        # drop the references (re-enabling the next operation).
        if self._op_thread is not None:
            self._op_thread.deleteLater()
        self._op_thread = None
        self._op_worker = None

    def _set_busy(self, busy: bool) -> None:
        # Disable the dialog buttons (Save/Close) too while an op runs: closing
        # the dialog mid-deploy would destroy the running QThread and abort the
        # process (the same crash FdtdWindow.closeEvent guards against).
        self.test_button.setEnabled(not busy)
        self.setup_button.setEnabled(not busy)
        self._buttons.setEnabled(not busy)

    def reject(self) -> None:
        if self._op_thread is not None:  # don't tear down a running op
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._op_thread is not None:
            event.ignore()  # block the window-manager close while busy
            return
        super().closeEvent(event)

    def _append(self, text: str) -> None:
        self.log.appendPlainText(text)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class FdtdWindow(QMainWindow):
    """Top-level FDTD simulation window. Two tabs: vertical mode profile and
    full 3D propagation. Field results rendered natively in a QGraphicsView
    (same Y-flipped coordinate system as the design canvas), so the viewport
    initialises to match wherever the user was looking in the design."""

    def __init__(self, document: LayoutDocument, view: LayoutView, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FDTD Simulation")
        self.document = document
        self.view = view

        self._mode_thread: QThread | None = None
        self._mode_worker: ModeWorker | None = None
        self._fdtd_thread: QThread | None = None
        self._fdtd_worker: FdtdWorker | None = None

        self._source_rows: list[dict] = []
        self._syncing_wavelength_energy = False
        self._syncing_run_time = False
        self._last_sim = None
        self._last_result = None
        self._region_um = None  # set per-run from "simulate selection only"
        self._field_origin_um = (0.0, 0.0)  # absolute centre of the field image
        self._last_params: FdtdParams | None = None
        self._field_image_initialized = False

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(100)
        self._play_timer.timeout.connect(self._advance_frame)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_mode_tab(), "Vertical Mode Profile")
        tabs.addTab(self._build_propagation_tab(), "Propagation (FDTD)")

        self.view.source_placement_requested.connect(self._on_source_placement_requested)

        # Seed both field views with the project's core index as the initial
        # phase index for µm→fs conversion; updated to n_eff after each mode solve.
        n0 = self.document.project_settings.core_index
        self.mode_view.set_n_eff(n0)
        self.run_view.set_n_eff(n0)

        # Re-apply the simulation set-up saved with this project (sources, run
        # parameters), if any. Only when one exists — otherwise the controls
        # keep their project-settings-seeded defaults above.
        if self.document.simulation_config is not None:
            self._restore_config(self.document.simulation_config)

        self.resize(700, 820)

    def closeEvent(self, event) -> None:
        # Capture the current set-up so it's saved even if the window is closed
        # before the next project save (the window instance is reused on reopen,
        # so its widgets persist, but the document copy must stay current too).
        self.sync_config_to_document()
        # The worker QThreads are parented to this window, so closing it while a
        # solve/run is still in flight would destroy a running thread — Qt aborts
        # the process ("QThread: Destroyed while thread is still running"), and a
        # GPU run mid-CUDA-call core-dumps. Disconnect the workers' result signals
        # first (so finished/failed don't fire callbacks against a window that's
        # tearing down), then let any active run finish. The compute loop isn't
        # interruptible, so wait() blocks until it returns — fast for a GPU run;
        # the run-time estimate already warns before slow CPU runs.
        for worker, thread in ((self._fdtd_worker, self._fdtd_thread), (self._mode_worker, self._mode_thread)):
            if thread is None or not thread.isRunning():
                continue
            if worker is not None:
                for sig in (worker.finished, worker.failed):
                    try:
                        sig.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            thread.quit()
            thread.wait()
        super().closeEvent(event)

    # -- mode profile tab ------------------------------------------------------

    def _build_mode_tab(self) -> QWidget:
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
        self.mode_wavelength_spin.setToolTip(
            "Free-space wavelength at which the vertical (cross-section) mode is solved. "
            "The resulting n_eff is wavelength-dependent, so this also sets the index used "
            "for the fs/ns axis rulers after a solve."
        )
        form.addRow("Wavelength (µm)", self.mode_wavelength_spin)

        self.mode_core_width_spin = QDoubleSpinBox()
        self.mode_core_width_spin.setDecimals(3)
        self.mode_core_width_spin.setRange(0.05, 50.0)
        self.mode_core_width_spin.setValue(0.5)
        self.mode_core_width_spin.setToolTip(
            "Lateral width of the waveguide core for this standalone cross-section solve "
            "(drawn as the core outline on the profile). Independent of the layout — set "
            "it to the width of the waveguide you want to characterise."
        )
        form.addRow("Core width (µm)", self.mode_core_width_spin)

        self.mode_num_modes_spin = QSpinBox()
        self.mode_num_modes_spin.setRange(1, 6)
        self.mode_num_modes_spin.setValue(1)
        self.mode_num_modes_spin.setToolTip(
            "How many guided modes to solve, ordered by n_eff. Mode 0 (the fundamental) "
            "is the one displayed; raise this to check whether higher-order modes are "
            "also supported at this width and wavelength."
        )
        form.addRow("Number of modes", self.mode_num_modes_spin)

        self.mode_clad_row = _CladRow(default_n=self.document.project_settings.clad_index)
        form.addRow("Cladding material", self.mode_clad_row)

        self.mode_units_combo = QComboBox()
        for label, _ in _UNIT_MODES:
            self.mode_units_combo.addItem(label)
        self.mode_units_combo.setToolTip(
            "Units for the profile's axis labels. The time modes (fs/ns) convert distance "
            "via the propagation time x·n_eff/c₀, using the n_eff from the last solve."
        )
        form.addRow("Axis units", self.mode_units_combo)

        layout.addLayout(form)

        self.mode_solve_button = QPushButton("Compute")
        self.mode_solve_button.setToolTip(
            "Solve the vertical mode profile for the cross-section above. Runs off the UI "
            "thread; on completion shows n_eff and a confinement check, and updates the "
            "fs/ns rulers in both tabs to the solved n_eff."
        )
        self.mode_solve_button.clicked.connect(self._on_solve_mode_clicked)
        layout.addWidget(self.mode_solve_button)

        self.mode_status_label = QLabel("")
        self.mode_status_label.setWordWrap(True)
        layout.addWidget(self.mode_status_label)

        self.mode_view = FieldView()
        self.mode_units_combo.currentIndexChanged.connect(
            lambda i: self.mode_view.set_unit_mode(_UNIT_MODES[i][1])
        )
        layout.addWidget(self.mode_view, stretch=1)

        return widget

    def _on_solve_mode_clicked(self) -> None:
        params = ModeProfileParams(
            wavelength_um=self.mode_wavelength_spin.value(),
            core_width_um=self.mode_core_width_spin.value(),
            num_modes=self.mode_num_modes_spin.value(),
            clad_index=self.mode_clad_row.clad_index(),
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
        self.mode_solve_button.setEnabled(True)
        check = mode_confinement(result, infinite_clad=self.document.project_settings.clad_infinite)
        n_eff = float(result.n_eff[0])
        self.mode_status_label.setText(
            f"n_eff = {n_eff:.4f}   ({elapsed:.2f} s)\n{check.message}"
        )

        # Update all views so the fs ruler uses the solved n_eff rather than
        # the bulk core index; propagate to the design canvas too so its
        # coordinate labels and status-bar cursor position stay consistent.
        self.mode_view.set_n_eff(n_eff)
        self.run_view.set_n_eff(n_eff)
        self.view.set_n_eff(n_eff)

        psi = abs(result.psi[0])          # (Ny, Nz) — lateral × vertical
        y_um = result.y * 1e6             # scene x axis
        z_um = result.z * 1e6             # scene y axis
        extent = (float(y_um[0]), float(y_um[-1]), float(z_um[0]), float(z_um[-1]))

        self.mode_view.set_image(psi, extent, _VIRIDIS, symmetric=False)
        self.mode_view.clear_overlays()

        cw = self.mode_core_width_spin.value()
        ct = self.document.project_settings.thickness_um
        self.mode_view.add_rect_overlay(-cw / 2, -ct / 2, cw, ct)  # core outline, accent color

        self.mode_view.fit_to_image()

    def _on_mode_failed(self, message: str) -> None:
        self.mode_solve_button.setEnabled(True)
        self.mode_status_label.setText(f"Error: {message}")

    # -- propagation tab -------------------------------------------------------

    def _build_propagation_tab(self) -> QWidget:
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
        self.run_wavelength_spin.setToolTip(
            "Default free-space wavelength used to seed each new source row's wavelength "
            "and to set the default cell size. Per-source wavelengths can still be edited "
            "in the table below."
        )
        form.addRow("Wavelength (µm)", self.run_wavelength_spin)

        self.run_cell_size_spin = QDoubleSpinBox()
        self.run_cell_size_spin.setDecimals(3)
        self.run_cell_size_spin.setRange(0.005, 1.0)
        default_params = FdtdParams(wavelength_um=self.run_wavelength_spin.value())
        self.run_cell_size_spin.setValue(default_params.resolved_cell_size_um())
        self.run_cell_size_spin.setToolTip(
            "Edge length of one Yee grid cell. Smaller cells resolve fine features more "
            "accurately but the cell count (and so memory and run time) grows as roughly "
            "1/size³ — the default is about λ/15."
        )
        form.addRow("Cell size (µm)", self.run_cell_size_spin)

        # Run time: spinbox + log-scale slider in one row
        rt_widget = QWidget()
        rt_layout = QHBoxLayout(rt_widget)
        rt_layout.setContentsMargins(0, 0, 0, 0)

        self.run_time_spin = QDoubleSpinBox()
        self.run_time_spin.setDecimals(1)
        self.run_time_spin.setRange(1.0, 1_000_000.0)  # up to 1 ns for very long runs
        self.run_time_spin.setSuffix(" fs")
        default_rt = default_params.resolved_run_time_fs()
        self.run_time_spin.setValue(default_rt)
        self.run_time_spin.setFixedWidth(110)
        self.run_time_spin.setToolTip(
            "Physical duration of the simulation in femtoseconds — how long the light is "
            "propagated, not wall-clock time. Allow enough for the field to traverse the "
            "domain; longer runs cost proportionally more steps."
        )
        rt_layout.addWidget(self.run_time_spin)

        self.run_time_slider = QSlider(Qt.Horizontal)
        self.run_time_slider.setRange(0, _RT_STEPS)
        self.run_time_slider.setValue(_fs_to_slider(default_rt))
        self.run_time_slider.setToolTip(
            "Log-scale run-time control, 10 fs to 100 ps, kept in sync with the box on the "
            "left. Type into the box for values beyond 100 ps."
        )
        rt_layout.addWidget(self.run_time_slider)

        form.addRow("Run time", rt_widget)

        self.run_clad_row = _CladRow(default_n=self.document.project_settings.clad_index)
        form.addRow("Cladding material", self.run_clad_row)

        if self.document.project_settings.clad_infinite:
            clad_thickness_text = "infinite (Project Settings)"
        else:
            clad_thickness_text = (
                f"{self.document.project_settings.clad_thickness_um:.3f} µm (Project Settings)"
            )
        self.run_clad_thickness_label = QLabel(clad_thickness_text)
        form.addRow("Cladding thickness", self.run_clad_thickness_label)

        self.run_units_combo = QComboBox()
        for label, _ in _UNIT_MODES:
            self.run_units_combo.addItem(label)
        self.run_units_combo.setToolTip(
            "Units for the field view's axis labels. The time modes (fs/ns) convert "
            "distance to propagation time using the last solved n_eff."
        )
        form.addRow("Axis units", self.run_units_combo)

        accel_widget = QWidget()
        accel_layout = QHBoxLayout(accel_widget)
        accel_layout.setContentsMargins(0, 0, 0, 0)
        # GPU/Numba are disabled unless their backend is actually importable —
        # photonfdtd silently ignores the request otherwise (it ANDs use_gpu
        # with availability), so a stray check would quietly run on the CPU.
        self.run_gpu_check = QCheckBox("GPU")
        if gpu_available():
            backend = gpu_backend_name()  # "CUDA" (NVIDIA) or "ROCm" (AMD)
            self.run_gpu_check.setToolTip(
                f"Run on the GPU via photonfdtd's CuPy backend ({backend})."
            )
        else:
            self.run_gpu_check.setEnabled(False)
            self.run_gpu_check.setToolTip(
                "GPU acceleration needs CuPy — either the CUDA build for an "
                "NVIDIA GPU (pip install cupy-cuda12x) or the ROCm build for an "
                "AMD GPU (pip install cupy-rocm-5-0). Not available in this "
                "environment."
            )
        self.run_numba_check = QCheckBox("Numba")
        if numba_available():
            # On by default: ~5x over plain NumPy and, unlike GPU, runs in the
            # worker thread so the UI doesn't freeze. (First-ever run JIT-compiles
            # the kernel — cached to disk, so only that one run is slow.)
            self.run_numba_check.setChecked(True)
            self.run_numba_check.setToolTip(
                "JIT-compile the update loop with Numba (~5x faster than NumPy, "
                "runs in the background). First run compiles and is slower; cached after."
            )
        else:
            self.run_numba_check.setEnabled(False)
            self.run_numba_check.setToolTip(
                "Numba acceleration needs the numba package (pip install numba) — "
                "not installed in this environment."
            )
        # photonfdtd 0.4's differentiable JAX backend. Exclusive of GPU/Numba
        # (photonfdtd raises otherwise), so ticking it clears those — see
        # _on_jax_toggled. Disabled unless jax is importable, since (unlike
        # GPU/Numba) photonfdtd does not silently fall back for JAX.
        self.run_jax_check = QCheckBox("JAX")
        if jax_available():
            self.run_jax_check.setToolTip(
                "Run on photonfdtd's differentiable JAX backend (0.4+). Exclusive "
                "of GPU/Numba; runs in the background like Numba. First run traces "
                "and compiles the stepper, so it's slower; cached after."
            )
        else:
            self.run_jax_check.setEnabled(False)
            self.run_jax_check.setToolTip(
                "The JAX backend needs the jax package (pip install jax) — not "
                "installed in this environment."
            )
        self.run_low_memory_check = QCheckBox("Low memory (disk)")
        self.run_low_memory_check.setToolTip(
            "Out-of-core stepping: stream the field arrays to scratch disk and step "
            "the grid a slab at a time, so peak RAM stays bounded by one tile and a "
            "grid too big for memory can still run. NumPy-only (overrides GPU/Numba) "
            "and slower — for when a run would otherwise run out of memory."
        )
        accel_layout.addWidget(self.run_gpu_check)
        accel_layout.addWidget(self.run_numba_check)
        accel_layout.addWidget(self.run_jax_check)
        accel_layout.addWidget(self.run_low_memory_check)
        accel_layout.addStretch(1)
        form.addRow("Acceleration", accel_widget)

        # Anisotropic subpixel smoothing (photonfdtd 0.4): sub-cell-accurate
        # permittivity at material interfaces — more accurate for a given cell
        # size. Incompatible with the Numba backend (photonfdtd raises), so
        # ticking it clears Numba, and vice versa (see _on_subpixel_toggled).
        self.run_subpixel_check = QCheckBox("Subpixel smoothing (more accurate)")
        self.run_subpixel_check.setToolTip(
            "Anisotropic subpixel smoothing of material interfaces (photonfdtd "
            "0.4): sub-cell-accurate permittivity at boundaries, so a given cell "
            "size resolves the geometry more faithfully — at some setup cost. "
            "Works on the NumPy, GPU and JAX backends, but not Numba."
        )
        form.addRow("Accuracy", self.run_subpixel_check)

        # Keep the mutually-exclusive backends consistent so a conflicting
        # combination never reaches photonfdtd (which raises on one). Each
        # handler only clears *other* boxes when its own is switched on, so the
        # resulting re-entrant toggled() signals (which arrive with checked=
        # False) are no-ops and can't loop.
        self.run_jax_check.toggled.connect(self._on_jax_toggled)
        self.run_gpu_check.toggled.connect(self._on_gpu_toggled)
        self.run_numba_check.toggled.connect(self._on_numba_toggled)
        self.run_subpixel_check.toggled.connect(self._on_subpixel_toggled)

        self.run_region_check = QCheckBox("Simulate selected components only")
        self.run_region_check.setToolTip(
            "Grid only the bounding box of the components selected on the canvas "
            "(plus your sources), instead of the whole layout — the way to keep a "
            "large chip from running out of memory. Light crossing the region edge "
            "is absorbed, so use it for a local look at a device, not a "
            "through-circuit measurement."
        )
        form.addRow("Region", self.run_region_check)

        remote_widget = QWidget()
        remote_layout = QHBoxLayout(remote_widget)
        remote_layout.setContentsMargins(0, 0, 0, 0)
        self.run_remote_check = QCheckBox("Run on remote server")
        self.run_remote_check.setToolTip(
            "Send this run to a remote machine over SSH (e.g. a GPU box) and "
            "fetch the result back, instead of computing locally. Configure the "
            "host and do the one-time setup with the button on the right."
        )
        remote_layout.addWidget(self.run_remote_check)
        self.remote_config_button = QPushButton("Configure…")
        self.remote_config_button.clicked.connect(self._on_configure_remote)
        remote_layout.addWidget(self.remote_config_button)
        remote_layout.addStretch(1)
        form.addRow("Remote", remote_widget)

        layout.addLayout(form)

        self.place_source_button = QPushButton("Place Source on Canvas")
        self.place_source_button.setCheckable(True)
        self.place_source_button.setToolTip(
            "Toggle source-placement mode: while active, clicking the design canvas drops "
            "a source at that point and adds a row to the table below. Untoggle to return "
            "the canvas to normal editing."
        )
        self.place_source_button.toggled.connect(self._on_place_source_toggled)
        layout.addWidget(self.place_source_button)

        self.source_table = QTableWidget(0, len(_TABLE_COLUMNS))
        self.source_table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        self.source_table.setMaximumHeight(140)
        self.source_table.setToolTip(
            "One row per placed source. Pick a Kind per row; cells that the chosen Kind "
            "doesn't use are greyed out. Add rows with 'Place Source on Canvas'."
        )
        self._init_source_table_header_tooltips()
        self.source_table.itemChanged.connect(self._on_source_table_item_changed)
        layout.addWidget(self.source_table)

        self.run_button = QPushButton("Compute")
        self.run_button.setToolTip(
            "Run the FDTD propagation with the parameters and sources above. Runs off the "
            "UI thread (locally, in a GPU subprocess, or on the remote host); a large grid "
            "prompts a memory/run-time warning first. Results animate in the view below."
        )
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)

        self.run_status_label = QLabel("")
        self.run_status_label.setWordWrap(True)
        layout.addWidget(self.run_status_label)

        # Progress bar for the running solve. Hidden until a run starts; shown
        # busy (indeterminate) during the startup/upload phase before the first
        # progress tick, then switched to a determinate 0–100% as ticks arrive
        # (the same signal feeds it for local, GPU-subprocess, and remote runs).
        self.run_progress = QProgressBar()
        self.run_progress.setTextVisible(True)
        self.run_progress.setVisible(False)
        layout.addWidget(self.run_progress)

        playback_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.setFixedWidth(60)
        self.play_button.setToolTip(
            "Animate the recorded field frames in a loop (toggles to Pause). Enabled once "
            "a run produces more than one frame."
        )
        self.play_button.toggled.connect(self._on_play_toggled)
        playback_row.addWidget(self.play_button)

        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setEnabled(False)
        self.frame_slider.setToolTip(
            "Scrub through the recorded field frames. A run records up to ~300 frames, "
            "sampled evenly across the full run time."
        )
        self.frame_slider.valueChanged.connect(self._on_slider_changed)
        playback_row.addWidget(self.frame_slider)

        playback_row.addWidget(QLabel("Speed"))
        self.play_speed_combo = QComboBox()
        for label, mult in (("0.25×", 0.25), ("0.5×", 0.5), ("1×", 1.0), ("2×", 2.0), ("4×", 4.0)):
            self.play_speed_combo.addItem(label, mult)
        self.play_speed_combo.setCurrentIndex(2)  # 1×
        self.play_speed_combo.setToolTip(
            "Playback speed multiplier (1× is 10 frames per second). Also sets the frame "
            "duration written into an exported GIF."
        )
        self.play_speed_combo.currentIndexChanged.connect(self._on_play_speed_changed)
        playback_row.addWidget(self.play_speed_combo)

        self.save_gif_button = QPushButton("Save GIF…")
        self.save_gif_button.setEnabled(False)
        self.save_gif_button.setToolTip(
            "Export every recorded frame (field plus the chip/source overlay, as shown) to "
            "an animated GIF, looping at the current Speed. Enabled after a run produces "
            "frames; requires Pillow."
        )
        self.save_gif_button.clicked.connect(self._on_save_gif)
        playback_row.addWidget(self.save_gif_button)
        layout.addLayout(playback_row)

        self.run_view = FieldView()
        self.run_units_combo.currentIndexChanged.connect(
            lambda i: self.run_view.set_unit_mode(_UNIT_MODES[i][1])
        )
        layout.addWidget(self.run_view, stretch=1)

        # Wire slider↔spinbox sync (re-entrancy guard: _syncing_run_time)
        self.run_time_spin.valueChanged.connect(self._on_run_time_spin_changed)
        self.run_time_slider.valueChanged.connect(self._on_run_time_slider_changed)

        return widget

    # -- run time slider sync --------------------------------------------------

    def _on_run_time_spin_changed(self, value: float) -> None:
        if self._syncing_run_time:
            return
        self._syncing_run_time = True
        try:
            self.run_time_slider.setValue(_fs_to_slider(value))
        finally:
            self._syncing_run_time = False

    def _on_run_time_slider_changed(self, v: int) -> None:
        if self._syncing_run_time:
            return
        self._syncing_run_time = True
        try:
            self.run_time_spin.setValue(_slider_to_fs(v))
        finally:
            self._syncing_run_time = False

    # -- source placement ------------------------------------------------------

    def _init_source_table_header_tooltips(self) -> None:
        """Per-column header tooltips explaining what each cell feeds and which
        source Kind consumes it (mirrors _RELEVANT_COLUMNS). Set on the header
        items rather than per-cell, since cells are recreated on every row add."""
        tips = {
            _COL_X: "Source X position in layout coordinates (µm). Seeded by the click "
                    "that placed it; editable.",
            _COL_Y: "Source Y position in layout coordinates (µm). Seeded by the click "
                    "that placed it; editable.",
            _COL_KIND: "Excitation type. 'dipole' is a plain point source; 'single_photon' "
                       "launches the local guided mode; 'scripted' uses a custom waveform; "
                       "'cherenkov' models a charged particle crossing the chip. The choice "
                       "decides which other cells in the row are active.",
            _COL_WAVELENGTH: "Free-space wavelength (µm) for this source. Paired with the "
                             "Energy cell — editing one rewrites the other. Used by dipole, "
                             "single_photon and cherenkov.",
            _COL_ENERGY: "Photon energy (eV), the inverse-wavelength view of this source. "
                         "Editing it rewrites the Wavelength cell and vice versa.",
            _COL_PHOTON_COUNT: "single_photon only: number of photons. Scales the launched "
                               "amplitude by √N (so intensity by N), not by stacking copies.",
            _COL_CORE_WIDTH: "single_photon only: width (µm) of the local guided mode solved "
                             "under the source and launched into the waveguide.",
            _COL_SCRIPT: "scripted only: a Python expression of t (seconds) giving the "
                         "source's time-domain waveform.",
            _COL_BETA: "cherenkov only: particle speed as a fraction of c (v/c). Cherenkov "
                       "emission needs β·n > 1.",
            _COL_TRACK_DIR: "cherenkov only: tilt of the particle track from vertical, in "
                            "degrees (0 = straight up, out of the layout plane).",
            _COL_TRACK_LEN: "cherenkov only: length of the track in z (µm), clamped to the "
                            "dielectric stack thickness.",
        }
        for col, text in tips.items():
            item = self.source_table.horizontalHeaderItem(col)
            if item is not None:
                item.setToolTip(text)

    def _on_place_source_toggled(self, checked: bool) -> None:
        self.view.set_source_mode(checked)

    def _on_source_placement_requested(self, x: float, y: float) -> None:
        marker = self.view.add_source_marker(x, y)
        row = self.source_table.rowCount()
        self.source_table.insertRow(row)
        self.source_table.setItem(row, _COL_X, QTableWidgetItem(f"{x:.4f}"))
        self.source_table.setItem(row, _COL_Y, QTableWidgetItem(f"{y:.4f}"))

        kind_combo = QComboBox()
        kind_combo.addItems(["dipole", "single_photon", "scripted", "cherenkov"])
        kind_combo.currentTextChanged.connect(lambda _text, c=kind_combo: self._on_kind_changed(c))
        self.source_table.setCellWidget(row, _COL_KIND, kind_combo)

        wavelength_um = self.run_wavelength_spin.value()
        self.source_table.setItem(row, _COL_WAVELENGTH, QTableWidgetItem(f"{wavelength_um:.4f}"))
        self.source_table.setItem(
            row, _COL_ENERGY, QTableWidgetItem(f"{photon_energy_ev_from_wavelength_um(wavelength_um):.4f}")
        )
        self.source_table.setItem(row, _COL_PHOTON_COUNT, QTableWidgetItem("1"))
        self.source_table.setItem(row, _COL_CORE_WIDTH, QTableWidgetItem("0.5"))
        self.source_table.setItem(row, _COL_SCRIPT, QTableWidgetItem(""))
        self.source_table.setItem(row, _COL_BETA, QTableWidgetItem("0.8"))
        self.source_table.setItem(row, _COL_TRACK_DIR, QTableWidgetItem("0.0"))
        self.source_table.setItem(row, _COL_TRACK_LEN, QTableWidgetItem("5.0"))

        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(lambda checked=False, m=marker: self._on_remove_source_row(m))
        self.source_table.setCellWidget(row, _COL_REMOVE, remove_button)

        self._source_rows.append({"marker": marker})
        self._apply_kind_to_row(row)  # grey out cells the default ("dipole") kind doesn't use

    def _on_kind_changed(self, combo: QComboBox) -> None:
        """Re-grey a row's parameter cells when its kind changes. The combo (not
        a fixed row index) is the handle, since rows shift as others are removed;
        find its current row, then re-apply."""
        for row in range(self.source_table.rowCount()):
            if self.source_table.cellWidget(row, _COL_KIND) is combo:
                self._apply_kind_to_row(row)
                return

    def _apply_kind_to_row(self, row: int) -> None:
        """Disable (grey, non-editable) the parameter cells that don't apply to
        this row's selected kind, leaving only the relevant ones live. Values are
        kept, not cleared, so switching kind back restores them. Qt renders a
        disabled item with the palette's faded text colour — that's the fade."""
        relevant = _RELEVANT_COLUMNS.get(self.source_table.cellWidget(row, _COL_KIND).currentText(), set())
        # Toggling flags on the wavelength/energy cells can re-emit itemChanged;
        # suppress the paired wavelength↔energy resync while we restyle.
        self._syncing_wavelength_energy = True
        try:
            for col in _PARAM_COLUMNS:
                item = self.source_table.item(row, col)
                if item is None:
                    continue
                if col in relevant:
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~(Qt.ItemIsEnabled | Qt.ItemIsEditable))
        finally:
            self._syncing_wavelength_energy = False

    def _on_remove_source_row(self, marker) -> None:
        for row_idx, row_data in enumerate(self._source_rows):
            if row_data["marker"] is marker:
                self.view.remove_source_marker(marker)
                self.source_table.removeRow(row_idx)
                del self._source_rows[row_idx]
                return

    def _on_source_table_item_changed(self, item: QTableWidgetItem) -> None:
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
            pass
        finally:
            self._syncing_wavelength_energy = False

    def _cell_float(self, row: int, col: int, default: float) -> float:
        """Parse a table cell as a float, falling back to ``default`` if the cell
        is blank, mid-edit, or missing. A single un-parseable cell must not sink
        the whole capture: _collect_source_specs feeds sync_config_to_document,
        which persists the sim set-up on save/close, and an exception there used
        to be swallowed — silently dropping *every* source. Defaulting the one
        bad cell keeps the rest."""
        item = self.source_table.item(row, col)
        try:
            return float(item.text())
        except (AttributeError, ValueError):
            return default

    def _cell_int(self, row: int, col: int, default: int) -> int:
        item = self.source_table.item(row, col)
        try:
            return int(item.text())
        except (AttributeError, ValueError):
            return default

    def _collect_source_specs(self) -> tuple[SourceSpec, ...]:
        specs = []
        for row in range(self.source_table.rowCount()):
            kind = self.source_table.cellWidget(row, _COL_KIND).currentText()
            core_width_um = (
                self._cell_float(row, _COL_CORE_WIDTH, 0.5)
                if kind == "single_photon" else None
            )
            script_item = self.source_table.item(row, _COL_SCRIPT)
            script = (
                (script_item.text() if script_item is not None else "")
                if kind == "scripted" else None
            )
            cherenkov_kwargs = {}
            if kind == "cherenkov":
                cherenkov_kwargs = dict(
                    velocity_beta=self._cell_float(row, _COL_BETA, 0.8),
                    direction_deg=self._cell_float(row, _COL_TRACK_DIR, 0.0),
                    cherenkov_length_um=self._cell_float(row, _COL_TRACK_LEN, 5.0),
                )
            specs.append(SourceSpec(
                x_um=self._cell_float(row, _COL_X, 0.0),
                y_um=self._cell_float(row, _COL_Y, 0.0),
                kind=kind,
                wavelength_um=self._cell_float(row, _COL_WAVELENGTH, 1.55),
                photon_count=self._cell_int(row, _COL_PHOTON_COUNT, 1),
                core_width_um=core_width_um, script=script,
                **cherenkov_kwargs,
            ))
        return tuple(specs)

    # -- persisted configuration ----------------------------------------------

    def sync_config_to_document(self) -> None:
        """Capture the window's current controls into document.simulation_config
        so the next project save persists them. The main window calls this
        before saving (and the window calls it on close): the live widgets, not
        a stale snapshot, are the source of truth while the window is open.

        Best-effort: a half-typed source cell makes _collect_source_specs raise
        (the table cells are free text with no validator). Persisting the sim
        set-up must never block the layout save or the window close that invoke
        this, so on a parse failure we keep the previous config and move on."""
        try:
            params = self._current_params()  # parses the free-text source cells
        except (ValueError, TypeError):
            return
        self.document.simulation_config = SimulationConfig(
            wavelength_um=params.wavelength_um,
            cell_size_um=params.cell_size_um,
            run_time_fs=params.run_time_fs,
            clad_index=params.clad_index,
            use_gpu=params.use_gpu,
            use_numba=params.use_numba,
            use_jax=params.use_jax,
            subpixel=params.subpixel,
            region_selected_only=self.run_region_check.isChecked(),
            sources=params.sources,
            mode_wavelength_um=self.mode_wavelength_spin.value(),
            mode_core_width_um=self.mode_core_width_spin.value(),
            mode_num_modes=self.mode_num_modes_spin.value(),
            mode_clad_index=self.mode_clad_row.clad_index(),
        )

    def _restore_config(self, config: SimulationConfig) -> None:
        """Apply a saved SimulationConfig to the controls (inverse of
        sync_config_to_document). Drives the source-of-truth widget and lets the
        existing handlers follow — setting the run-time spinbox moves its slider,
        setting a source row's wavelength updates its energy cell, etc."""
        self.run_wavelength_spin.setValue(config.wavelength_um)
        if config.cell_size_um is not None:
            self.run_cell_size_spin.setValue(config.cell_size_um)
        if config.run_time_fs is not None:
            self.run_time_spin.setValue(config.run_time_fs)  # slider follows via signal
        if config.clad_index is not None:
            self.run_clad_row.set_clad_index(config.clad_index)
        # Only toggle accelerators whose backend is actually available, so a
        # project saved on a GPU box doesn't tick a disabled box on a CPU box.
        if self.run_gpu_check.isEnabled():
            self.run_gpu_check.setChecked(config.use_gpu)
        if self.run_numba_check.isEnabled():
            self.run_numba_check.setChecked(config.use_numba)
        # New in 0.4; getattr keeps projects saved by an older phidler (whose
        # SimulationConfig had no such field) loading cleanly.
        if self.run_jax_check.isEnabled():
            self.run_jax_check.setChecked(getattr(config, "use_jax", False))
        self.run_subpixel_check.setChecked(getattr(config, "subpixel", False))
        self.run_region_check.setChecked(config.region_selected_only)
        for spec in config.sources:
            self._restore_source(spec)

        self.mode_wavelength_spin.setValue(config.mode_wavelength_um)
        self.mode_core_width_spin.setValue(config.mode_core_width_um)
        self.mode_num_modes_spin.setValue(config.mode_num_modes)
        if config.mode_clad_index is not None:
            self.mode_clad_row.set_clad_index(config.mode_clad_index)

    def _restore_source(self, spec: SourceSpec) -> None:
        """Recreate one saved source: place its canvas marker and table row via
        the normal placement path, then fill the row's cells from the spec."""
        self._on_source_placement_requested(spec.x_um, spec.y_um)
        row = self.source_table.rowCount() - 1
        self.source_table.cellWidget(row, _COL_KIND).setCurrentText(spec.kind)
        self.source_table.item(row, _COL_WAVELENGTH).setText(f"{spec.wavelength_um:.4f}")  # energy follows
        self.source_table.item(row, _COL_PHOTON_COUNT).setText(str(spec.photon_count))
        if spec.core_width_um is not None:
            self.source_table.item(row, _COL_CORE_WIDTH).setText(f"{spec.core_width_um:.4f}")
        if spec.script is not None:
            self.source_table.item(row, _COL_SCRIPT).setText(spec.script)
        self.source_table.item(row, _COL_BETA).setText(f"{spec.velocity_beta}")
        self.source_table.item(row, _COL_TRACK_DIR).setText(f"{spec.direction_deg}")
        self.source_table.item(row, _COL_TRACK_LEN).setText(f"{spec.cherenkov_length_um}")

    # -- backend exclusivity ---------------------------------------------------

    def _on_jax_toggled(self, checked: bool) -> None:
        # JAX is exclusive of the GPU and Numba backends.
        if checked:
            self.run_gpu_check.setChecked(False)
            self.run_numba_check.setChecked(False)

    def _on_gpu_toggled(self, checked: bool) -> None:
        # GPU is exclusive of JAX.
        if checked:
            self.run_jax_check.setChecked(False)

    def _on_numba_toggled(self, checked: bool) -> None:
        # Numba is exclusive of JAX and can't do subpixel smoothing.
        if checked:
            self.run_jax_check.setChecked(False)
            self.run_subpixel_check.setChecked(False)

    def _on_subpixel_toggled(self, checked: bool) -> None:
        # Subpixel smoothing is unsupported on the Numba backend.
        if checked:
            self.run_numba_check.setChecked(False)

    # -- running the simulation ------------------------------------------------

    def _current_params(self) -> FdtdParams:
        return FdtdParams(
            wavelength_um=self.run_wavelength_spin.value(),
            cell_size_um=self.run_cell_size_spin.value(),
            run_time_fs=self.run_time_spin.value(),
            sources=self._collect_source_specs(),
            clad_index=self.run_clad_row.clad_index(),
            use_gpu=self.run_gpu_check.isChecked(),
            use_numba=self.run_numba_check.isChecked(),
            use_jax=self.run_jax_check.isChecked(),
            subpixel=self.run_subpixel_check.isChecked(),
            out_of_core=self.run_low_memory_check.isChecked(),
        )

    def _selected_region_um(self):
        """Bounding box (left, bottom, right, top in µm, with a margin) of the
        components selected on the canvas plus the placed sources — the xy
        window to simulate. None if nothing is selected. Whole instances, so the
        region never cuts through the middle of a device."""
        scene = self.view.scene()
        ids = [
            it.inst_id
            for it in scene.selectedItems()
            if hasattr(it, "inst_id") and it.inst_id in self.document.instances
        ]
        if not ids:
            return None
        lefts, bottoms, rights, tops = [], [], [], []
        for inst_id in ids:
            bb = self.document.instances[inst_id].ref.dbbox()
            lefts.append(bb.left); bottoms.append(bb.bottom); rights.append(bb.right); tops.append(bb.top)
        for spec in self._collect_source_specs():  # keep sources inside the grid
            lefts.append(spec.x_um); rights.append(spec.x_um)
            bottoms.append(spec.y_um); tops.append(spec.y_um)
        m = _REGION_MARGIN_UM
        return (min(lefts) - m, min(bottoms) - m, max(rights) + m, max(tops) + m)

    def _on_configure_remote(self) -> None:
        """Open the remote-server setup dialog (host, paths, test/deploy)."""
        dialog = RemoteConfigDialog(self)
        dialog.exec()

    def _offer_feasible_region(self, params, region_um, cell_count):
        """If a run this size can't fit (RAM in-core, scratch disk out-of-core),
        offer to simulate a runnable region around the sources (or the current
        selection) instead of launching a doomed run. Returns
        ``(region_um, cell_count, confirmed)`` to proceed with — possibly the
        shrunk region — or ``None`` if the user declined. ``confirmed`` is True
        when the user accepted the suggested region (so the caller can skip the
        redundant large-run warning). Split out so it's testable by monkeypatching
        QMessageBox rather than driving the modal dialog."""
        try:
            check_run_feasible(cell_count, params)
        except RuntimeError as exc:
            center = None
            if region_um is not None:  # an already-selected region was still too big
                center = ((region_um[0] + region_um[2]) / 2, (region_um[1] + region_um[3]) / 2)
            # Cap the suggestion so it both fits and runs in reasonable time, not
            # just "the biggest thing that fits in memory" (which is still slow).
            budget = min(feasible_cell_budget(params), _SUGGESTED_MAX_CELLS)
            suggested = suggest_region_um(self.document, params, budget, center_um=center)
            w, h = suggested[2] - suggested[0], suggested[3] - suggested[1]
            where = "the selection" if region_um is not None else "the sources"
            reply = QMessageBox.question(
                self, "Layout too large to simulate whole",
                f"{exc}\n\nSimulate a {w:.0f}×{h:.0f} µm region around {where} instead?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return None
            new_count = estimate_grid_cell_count(self.document, params, region_um=suggested)
            return suggested, new_count, True
        return region_um, cell_count, False

    def _on_run_clicked(self) -> None:
        params = self._current_params()

        remote = self.run_remote_check.isChecked()
        remote_cfg = None
        if remote:
            from phidler.remote_config import load_remote_config

            remote_cfg = load_remote_config()
            if not remote_cfg.is_configured():
                QMessageBox.warning(
                    self, "Remote not configured",
                    "No remote server is set up. Click 'Configure…' next to "
                    "'Run on remote server' to set a host and run the one-time setup, "
                    "or untick it to run locally.",
                )
                return
            # The local GPU checkbox is disabled when this machine has no GPU, so
            # _current_params() always reports use_gpu=False. For a remote run the
            # *remote's* GPU is what matters, so take the toggle from the config.
            params = dataclasses.replace(params, use_gpu=remote_cfg.use_gpu)
        self._last_params = params

        region_um = None
        if self.run_region_check.isChecked():
            region_um = self._selected_region_um()
            if region_um is None:
                QMessageBox.warning(
                    self, "No selection",
                    "Select one or more components on the canvas to simulate only that "
                    "region, or untick 'Simulate selected components only'.",
                )
                return
        self._region_um = region_um

        try:
            cell_count = estimate_grid_cell_count(self.document, params, region_um=region_um)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot run simulation", str(exc))
            return

        # If the grid is too big to run (a whole large chip like the TDC example
        # is billions of cells), don't launch a doomed run — offer a runnable
        # region around the sources (or the current selection) instead.
        resolved = self._offer_feasible_region(params, region_um, cell_count)
        if resolved is None:  # user declined the offer
            return
        region_um, cell_count, region_confirmed = resolved

        cell_size_m = params.resolved_cell_size_um() * 1e-6
        courant = 0.99
        dt = courant / (299_792_458.0 * (3 ** 0.5) / cell_size_m)
        n_steps = int(params.resolved_run_time_fs() * 1e-15 / dt) + 1

        # Stretch the monitor interval on long runs so the movie stays ~a few
        # hundred frames instead of one per few steps (which would be unusable
        # and eat memory). Short runs keep the fine default interval.
        interval = max(params.monitor_interval, math.ceil(n_steps / _MAX_MOVIE_FRAMES))
        if interval != params.monitor_interval:
            params = dataclasses.replace(params, monitor_interval=interval)
            self._last_params = params

        grid = (cell_count, 1, 1)
        memory_gb = estimate_memory_gb(cell_count)
        selected = "gpu" if params.use_gpu else ("numba" if params.use_numba else "numpy")
        selected_seconds = estimate_run_seconds(grid, n_steps, backend=selected)

        # The run-time estimates are calibrated on *local* hardware, so they're
        # meaningless for a remote box — show only the (machine-independent)
        # memory figure on the remote path, and only when it's genuinely large.
        if remote:
            if memory_gb > _MEMORY_WARNING_GB:
                reply = QMessageBox.question(
                    self, "Large simulation",
                    f"This grid has about {cell_count:,} cells and needs roughly "
                    f"{memory_gb:.1f} GB of memory — make sure the remote host has "
                    f"enough (try a coarser cell size, or simulate a smaller region). "
                    f"Continue?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
        elif region_confirmed:
            pass  # user already accepted this (feasibility-sized) region — don't double-prompt
        elif selected_seconds > _RUN_TIME_WARNING_SECONDS or memory_gb > _MEMORY_WARNING_GB:
            t_numba = estimate_run_seconds(grid, n_steps, "numba")
            t_gpu = estimate_run_seconds(grid, n_steps, "gpu")
            t_numpy = estimate_run_seconds(grid, n_steps, "numpy")
            mark = {"numba": "Numba", "gpu": "GPU", "numpy": "plain NumPy"}[selected]
            reply = QMessageBox.question(
                self, "Large simulation",
                f"This grid has about {cell_count:,} cells and needs roughly "
                f"{memory_gb:.1f} GB of memory — large grids can run out of memory "
                f"(try a coarser cell size, or simulate a smaller region).\n\n"
                f"Estimated run time:\n"
                f"    Numba (CPU):  ~{t_numba:.0f} s\n"
                f"    GPU:  ~{t_gpu:.0f} s\n"
                f"    plain NumPy:  ~{t_numpy:.0f} s\n\n"
                f"You have {mark} selected. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        # GPU runs spawn a child process and pay its ~1 s startup, so say so —
        # otherwise the extra second reads as the app hanging. Remote runs also
        # pay an upload/connect cost, so flag that too.
        if remote:
            self.run_status_label.setText("Running on remote server…")
        else:
            self.run_status_label.setText("Running on GPU…" if params.use_gpu else "Running…")
        self._field_image_initialized = False

        # Start the progress bar busy (indeterminate): there's a startup phase —
        # JIT compile, GPU/subprocess launch, or remote upload — before the first
        # step tick, and an animated bar there reads as "working" rather than
        # stuck at 0%. _on_fdtd_progress switches it to a determinate 0–100% once
        # ticks start arriving.
        self.run_progress.setRange(0, 0)
        self.run_progress.setVisible(True)

        # Both backends run in a worker thread now: the non-GPU path computes
        # there directly; the GPU path waits there on a child process, and the
        # remote path waits on ssh (see FdtdWorker.run). Either way the UI stays live.
        self._fdtd_thread = QThread(self)
        self._fdtd_worker = FdtdWorker(self.document, params, region_um, remote=remote, remote_cfg=remote_cfg)
        self._fdtd_worker.moveToThread(self._fdtd_thread)
        self._fdtd_thread.started.connect(self._fdtd_worker.run)
        self._fdtd_worker.progress.connect(self._on_fdtd_progress)
        self._fdtd_worker.finished.connect(self._on_fdtd_finished)
        self._fdtd_worker.failed.connect(self._on_fdtd_failed)
        self._fdtd_worker.finished.connect(self._fdtd_thread.quit)
        self._fdtd_worker.failed.connect(self._fdtd_thread.quit)
        self._fdtd_thread.start()

    def _on_fdtd_progress(self, step: int, n_steps: int) -> None:
        # First real tick: leave busy/indeterminate mode for a determinate bar.
        if self.run_progress.maximum() == 0:
            self.run_progress.setRange(0, 100)
        pct = int(100 * step / n_steps) if n_steps > 0 else 0
        self.run_progress.setValue(max(0, min(100, pct)))

    def _on_fdtd_finished(self, sim, result, elapsed: float) -> None:
        self.run_button.setEnabled(True)
        self.run_progress.setVisible(False)
        self._last_sim = sim
        self._last_result = result

        arr = result.fields["field"]["Ez"]
        n_frames = arr.shape[0]
        gx, gy, gz = (int(s) for s in sim.grid.shape)  # full sim grid (the movie keeps only z=0)
        # Report the backend that actually ran (not what was requested) — a GPU
        # request can quietly fall back to CPU, which this makes visible.
        if getattr(sim, "use_gpu", False):
            # Name the GPU vendor backend (CUDA/ROCm) when we can identify it.
            gpu_kind = gpu_backend_name()
            backend = f"GPU ({gpu_kind})" if gpu_kind and gpu_kind != "GPU" else "GPU"
        elif getattr(sim, "use_numba", False):
            backend = "Numba"
        elif getattr(sim, "use_jax", False):
            backend = "JAX"
        else:
            backend = "CPU"
        if getattr(sim, "subpixel", False):
            backend += " + subpixel"
        self.run_status_label.setText(
            f"Done on {backend} in {elapsed:.2f} s — {n_frames} frames, grid {gx}×{gy}×{gz}"
        )

        self.frame_slider.setEnabled(n_frames > 1)
        self.frame_slider.setRange(0, max(n_frames - 1, 0))
        self.play_button.setEnabled(n_frames > 1)
        self.save_gif_button.setEnabled(n_frames > 0)

        # from_gdsfactory centres the FDTD domain on the (region or layout) bbox
        # centre, so grid.coords come back centred on 0 while the chip outline and
        # source markers are in absolute layout coords. Record that centre so the
        # field image can be shifted back into absolute coords and line up.
        if self._region_um is not None:
            left, bottom, right, top = self._region_um
        else:
            bb = self.document.top.bbox()
            left, bottom, right, top = bb.left, bb.bottom, bb.right, bb.top
        self._field_origin_um = ((left + right) / 2.0, (bottom + top) / 2.0)

        # Draw chip outlines and source markers (once per run)
        self.run_view.clear_overlays()
        shapes = shapes_for_cell(self.document.top)
        for shapes_list in shapes.values():
            for hull, _holes in shapes_list:
                self.run_view.add_polygon_overlay(hull)
        if self._last_params is not None:
            for src in self._last_params.sources:
                self.run_view.add_source_marker(src.x_um, src.y_um)

        # Render first frame then initialise viewport. For a region run, frame
        # the simulated region (the field image) — copying the design canvas's
        # viewport would show the whole chip with the ROI field as a tiny sliver.
        self.frame_slider.setValue(0)
        self._draw_frame(0)
        if getattr(self, "_region_um", None) is not None:
            self.run_view.fit_to_image()
        else:
            self.run_view.copy_viewport_from(self.view)

    def _on_fdtd_failed(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.run_progress.setVisible(False)
        self.run_status_label.setText(f"Error: {message}")

    # -- frame animation -------------------------------------------------------

    def _on_slider_changed(self, value: int) -> None:
        self._draw_frame(value)

    def _play_interval_ms(self) -> int:
        speed = self.play_speed_combo.currentData() or 1.0
        return max(10, round(_BASE_PLAY_INTERVAL_MS / speed))

    def _on_play_speed_changed(self, _index: int) -> None:
        self._play_timer.setInterval(self._play_interval_ms())

    def _on_play_toggled(self, checked: bool) -> None:
        if checked:
            self._play_timer.setInterval(self._play_interval_ms())
            self._play_timer.start()
            self.play_button.setText("Pause")
        else:
            self._play_timer.stop()
            self.play_button.setText("Play")

    def _advance_frame(self) -> None:
        n_frames = self.frame_slider.maximum() + 1
        self.frame_slider.setValue((self.frame_slider.value() + 1) % max(n_frames, 1))

    def _on_save_gif(self) -> None:
        if self._last_result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save GIF", "simulation.gif", "GIF (*.gif)")
        if not path:
            return
        if not path.lower().endswith(".gif"):
            path += ".gif"
        try:
            n = self._export_gif(path)
        except Exception as exc:  # PIL missing, disk error, etc.
            QMessageBox.critical(self, "Save GIF failed", str(exc))
            return
        self.run_status_label.setText(f"Saved {n}-frame GIF to {path}")

    def _export_gif(self, path: str) -> int:
        """Render every frame of the current result (field + chip overlay, as
        shown) into an animated GIF, played at the current Speed. Returns the
        frame count."""
        arr = self._last_result.fields["field"]["Ez"]
        n_frames = int(arr.shape[0])
        restore = self.frame_slider.value()
        frames = []
        for i in range(n_frames):
            self._draw_frame(i)
            QApplication.processEvents()  # let the view repaint before grabbing
            frames.append(self._qpixmap_to_pil(self.run_view.grab()))
        self._draw_frame(restore)

        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            duration=self._play_interval_ms(),  # ms per frame, matches playback speed
            loop=0,
        )
        return n_frames

    @staticmethod
    def _qpixmap_to_pil(pixmap):
        from PIL import Image

        image = pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
        width, height = image.width(), image.height()
        buffer = bytes(image.constBits())[: width * height * 4]
        return Image.frombytes("RGBA", (width, height), buffer).convert("RGB")

    def _draw_frame(self, frame_index: int) -> None:
        if self._last_result is None or self._last_sim is None:
            return
        arr = self._last_result.fields["field"]["Ez"]
        # The monitor records only the mid-core (z=0) plane, so the z axis is
        # size-1; locate the plane for a full-volume result (older saves/tests).
        z_idx = 0 if arr.shape[3] == 1 else nearest_z_index(self._last_sim.grid, 0.0)
        frame = arr[frame_index, :, :, z_idx]          # (Nx, Ny)

        # Shift the centred grid coords back to absolute layout coords so the
        # field image registers with the chip outline (see _on_fdtd_finished).
        origin_x, origin_y = getattr(self, "_field_origin_um", (0.0, 0.0))
        x_coords = self._last_sim.grid.coords[0] * 1e6 + origin_x
        y_coords = self._last_sim.grid.coords[1] * 1e6 + origin_y
        extent = (
            float(x_coords[0]), float(x_coords[-1]),
            float(y_coords[0]), float(y_coords[-1]),
        )

        if not self._field_image_initialized:
            self.run_view.set_image(frame, extent, _RDBU, symmetric=True)
            self._field_image_initialized = True
        else:
            self.run_view.update_image(frame, _RDBU, symmetric=True)

    # -- lifecycle -------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._play_timer.stop()
        self.view.set_source_mode(False)
        self.view.clear_source_markers()
        super().closeEvent(event)
