"""FDTD simulation window — native Qt rendering, no matplotlib dependency."""
from __future__ import annotations

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
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGraphicsScene,
    QGraphicsView,
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

from phidler.canvas.view import C0_UM_PER_FS, C0_UM_PER_NS, UNIT_MODES, LayoutView, nice_ticks
from phidler.fdtd_sim import (
    DISCLAIMER,
    FdtdParams,
    ModeProfileParams,
    SourceSpec,
    build_mode_solver,
    build_simulation,
    estimate_grid_cell_count,
    estimate_run_seconds,
    gpu_available,
    mode_confinement,
    nearest_z_index,
    numba_available,
    photon_energy_ev_from_wavelength_um,
    run_simulation,
    solve_mode_profile,
    wavelength_um_from_photon_energy_ev,
)
from phidler.model.document import LayoutDocument, shapes_for_cell

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
_RT_MAX_FS = 1_000.0
_RT_STEPS = 200


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

        self.resize(700, 820)

    def closeEvent(self, event) -> None:
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

        self.mode_clad_row = _CladRow(default_n=self.document.project_settings.clad_index)
        form.addRow("Cladding material", self.mode_clad_row)

        self.mode_units_combo = QComboBox()
        for label, _ in _UNIT_MODES:
            self.mode_units_combo.addItem(label)
        form.addRow("Axis units", self.mode_units_combo)

        layout.addLayout(form)

        self.mode_solve_button = QPushButton("Compute")
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
        form.addRow("Wavelength (µm)", self.run_wavelength_spin)

        self.run_cell_size_spin = QDoubleSpinBox()
        self.run_cell_size_spin.setDecimals(3)
        self.run_cell_size_spin.setRange(0.005, 1.0)
        default_params = FdtdParams(wavelength_um=self.run_wavelength_spin.value())
        self.run_cell_size_spin.setValue(default_params.resolved_cell_size_um())
        form.addRow("Cell size (µm)", self.run_cell_size_spin)

        # Run time: spinbox + log-scale slider in one row
        rt_widget = QWidget()
        rt_layout = QHBoxLayout(rt_widget)
        rt_layout.setContentsMargins(0, 0, 0, 0)

        self.run_time_spin = QDoubleSpinBox()
        self.run_time_spin.setDecimals(1)
        self.run_time_spin.setRange(1.0, 100_000.0)
        self.run_time_spin.setSuffix(" fs")
        default_rt = default_params.resolved_run_time_fs()
        self.run_time_spin.setValue(default_rt)
        self.run_time_spin.setFixedWidth(110)
        rt_layout.addWidget(self.run_time_spin)

        self.run_time_slider = QSlider(Qt.Horizontal)
        self.run_time_slider.setRange(0, _RT_STEPS)
        self.run_time_slider.setValue(_fs_to_slider(default_rt))
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
        form.addRow("Axis units", self.run_units_combo)

        accel_widget = QWidget()
        accel_layout = QHBoxLayout(accel_widget)
        accel_layout.setContentsMargins(0, 0, 0, 0)
        # GPU/Numba are disabled unless their backend is actually importable —
        # photonfdtd silently ignores the request otherwise (it ANDs use_gpu
        # with availability), so a stray check would quietly run on the CPU.
        self.run_gpu_check = QCheckBox("GPU")
        if gpu_available():
            self.run_gpu_check.setToolTip("Run on the GPU via photonfdtd's CuPy backend.")
        else:
            self.run_gpu_check.setEnabled(False)
            self.run_gpu_check.setToolTip(
                "GPU acceleration needs CuPy and a CUDA-capable NVIDIA GPU "
                "(pip install cupy-cuda12x) — not available in this environment."
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
        accel_layout.addWidget(self.run_gpu_check)
        accel_layout.addWidget(self.run_numba_check)
        accel_layout.addStretch(1)
        form.addRow("Acceleration", accel_widget)

        layout.addLayout(form)

        self.place_source_button = QPushButton("Place Source on Canvas")
        self.place_source_button.setCheckable(True)
        self.place_source_button.toggled.connect(self._on_place_source_toggled)
        layout.addWidget(self.place_source_button)

        self.source_table = QTableWidget(0, len(_TABLE_COLUMNS))
        self.source_table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        self.source_table.setMaximumHeight(140)
        self.source_table.itemChanged.connect(self._on_source_table_item_changed)
        layout.addWidget(self.source_table)

        self.run_button = QPushButton("Compute")
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)

        self.run_status_label = QLabel("")
        self.run_status_label.setWordWrap(True)
        layout.addWidget(self.run_status_label)

        playback_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.setFixedWidth(60)
        self.play_button.toggled.connect(self._on_play_toggled)
        playback_row.addWidget(self.play_button)

        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setEnabled(False)
        self.frame_slider.valueChanged.connect(self._on_slider_changed)
        playback_row.addWidget(self.frame_slider)
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

    def _collect_source_specs(self) -> tuple[SourceSpec, ...]:
        specs = []
        for row in range(self.source_table.rowCount()):
            x_um = float(self.source_table.item(row, _COL_X).text())
            y_um = float(self.source_table.item(row, _COL_Y).text())
            kind = self.source_table.cellWidget(row, _COL_KIND).currentText()
            wavelength_um = float(self.source_table.item(row, _COL_WAVELENGTH).text())
            photon_count = int(self.source_table.item(row, _COL_PHOTON_COUNT).text())
            core_width_um = (
                float(self.source_table.item(row, _COL_CORE_WIDTH).text())
                if kind == "single_photon" else None
            )
            script = (
                self.source_table.item(row, _COL_SCRIPT).text()
                if kind == "scripted" else None
            )
            cherenkov_kwargs = {}
            if kind == "cherenkov":
                cherenkov_kwargs = dict(
                    velocity_beta=float(self.source_table.item(row, _COL_BETA).text()),
                    direction_deg=float(self.source_table.item(row, _COL_TRACK_DIR).text()),
                    cherenkov_length_um=float(self.source_table.item(row, _COL_TRACK_LEN).text()),
                )
            specs.append(SourceSpec(
                x_um=x_um, y_um=y_um, kind=kind,
                wavelength_um=wavelength_um, photon_count=photon_count,
                core_width_um=core_width_um, script=script,
                **cherenkov_kwargs,
            ))
        return tuple(specs)

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
        )

    def _on_run_clicked(self) -> None:
        params = self._current_params()
        self._last_params = params
        try:
            cell_count = estimate_grid_cell_count(self.document, params)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot run simulation", str(exc))
            return

        cell_size_m = params.resolved_cell_size_um() * 1e-6
        courant = 0.99
        dt = courant / (299_792_458.0 * (3 ** 0.5) / cell_size_m)
        n_steps = int(params.resolved_run_time_fs() * 1e-15 / dt) + 1
        estimated_seconds = estimate_run_seconds((cell_count, 1, 1), n_steps)

        if estimated_seconds > _RUN_TIME_WARNING_SECONDS:
            reply = QMessageBox.question(
                self, "Large simulation",
                f"This grid has about {cell_count:,} cells and is estimated to take "
                f"roughly {estimated_seconds:.0f} s (NumPy backend, no GPU). Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.run_button.setEnabled(False)
        self.run_status_label.setText("Running…")
        self._field_image_initialized = False

        if params.use_gpu:
            # cupy's CUDA context, created inside a Qt worker thread, crashes /
            # hangs the process at teardown. GPU runs are fast (~1 s), so run
            # synchronously on the main thread instead — the UI blocks briefly,
            # which is a fine trade for not core-dumping.
            from PySide6.QtWidgets import QApplication

            self.run_status_label.repaint()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                t0 = time.time()
                sim = build_simulation(self.document, params)
                result = run_simulation(sim)
                elapsed = time.time() - t0
            except Exception as exc:
                QApplication.restoreOverrideCursor()
                self._on_fdtd_failed(str(exc))
                return
            QApplication.restoreOverrideCursor()
            self._on_fdtd_finished(sim, result, elapsed)
            return

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

        arr = result.fields["field"]["Ez"]
        n_frames = arr.shape[0]
        self.run_status_label.setText(
            f"Done in {elapsed:.2f} s — {n_frames} frames, "
            f"grid {arr.shape[1]}×{arr.shape[2]}×{arr.shape[3]}"
        )

        self.frame_slider.setEnabled(n_frames > 1)
        self.frame_slider.setRange(0, max(n_frames - 1, 0))
        self.play_button.setEnabled(n_frames > 1)

        # Draw chip outlines and source markers (once per run)
        self.run_view.clear_overlays()
        shapes = shapes_for_cell(self.document.top)
        for shapes_list in shapes.values():
            for hull, _holes in shapes_list:
                self.run_view.add_polygon_overlay(hull)
        if self._last_params is not None:
            for src in self._last_params.sources:
                self.run_view.add_source_marker(src.x_um, src.y_um)

        # Render first frame then initialise viewport
        self.frame_slider.setValue(0)
        self._draw_frame(0)
        self.run_view.copy_viewport_from(self.view)

    def _on_fdtd_failed(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.run_status_label.setText(f"Error: {message}")

    # -- frame animation -------------------------------------------------------

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
        self.frame_slider.setValue((self.frame_slider.value() + 1) % max(n_frames, 1))

    def _draw_frame(self, frame_index: int) -> None:
        if self._last_result is None or self._last_sim is None:
            return
        arr = self._last_result.fields["field"]["Ez"]
        z_idx = nearest_z_index(self._last_sim.grid, 0.0)
        frame = arr[frame_index, :, :, z_idx]          # (Nx, Ny)

        x_coords = self._last_sim.grid.coords[0] * 1e6
        y_coords = self._last_sim.grid.coords[1] * 1e6
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
