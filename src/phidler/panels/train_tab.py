"""The FDTD window's Train tab: fit a design box on the layout to a task the way
a neural network is fitted to data (see phidler.chip_train for the physics).

The user marks a design box on the canvas, picks which open waveguide ports are
inputs and outputs, chooses a task (the built-in priority encoder, a dataset
file, or a target transfer matrix) and trains. Each step's loss, task metric and
the evolving etch pattern stream back from a worker thread; the finished design
can be placed on the layout as a ``trained_design`` instance, undoable like any
other placement.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from phidler import chip_train as ct

_ROLES = ("—", "Input", "Output")
_BOX_COLOR = "#ff9800"

# Task combo entries: (label, Task kind, stacked-page index).
_TASKS = (
    ("Priority encoder (built-in)", "priority", 0),
    ("Classify a dataset (CSV / NPZ)", "classify", 1),
    ("Output power fractions (CSV / NPZ)", "regress", 1),
    ("Target transfer matrix", "transfer", 2),
)

_MATRIX_PRESETS = {
    "2x2 50:50 coupler": "1 1j\n1j 1",
    "4-point DFT": "1 1 1 1\n1 -1j -1 1j\n1 -1 1 -1\n1 1j -1 -1j",
    "2x2 cross (swap)": "0 1\n1 0",
}


def parse_matrix(text: str) -> np.ndarray:
    """A complex matrix from text: one row per line, entries separated by
    spaces or commas, each anything ``complex()`` reads (``1``, ``-1j``,
    ``0.5+0.5j``)."""
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append([complex(tok) for tok in line.replace(",", " ").split()])
    if not rows or len({len(r) for r in rows}) != 1:
        raise ValueError("enter one row per line, every row the same length")
    return np.asarray(rows, complex)


def _design_image(rho: np.ndarray) -> QImage:
    """Grayscale image of a (nx, ny) density, core light, etched dark, with y
    up (row 0 of the image is the top of the box)."""
    a = np.clip(np.asarray(rho, float), 0.0, 1.0)
    pix = np.ascontiguousarray((40 + 200 * a.T[::-1]).astype(np.uint8))
    h, w = pix.shape
    return QImage(pix.data, w, h, w, QImage.Format_Grayscale8).copy()


class CurvePlot(QWidget):
    """A small loss curve (left axis) with the task metric (right axis, 0-1)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.loss: list[float] = []
        self.metric_values: list[float] = []
        self.metric_name = ""
        self.setMinimumHeight(150)

    def clear(self, metric_name: str = "") -> None:
        self.loss, self.metric_values, self.metric_name = [], [], metric_name
        self.update()

    def add(self, loss: float, metric: float | None) -> None:
        self.loss.append(float(loss))
        if metric is not None:
            self.metric_values.append(float(metric))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        m = 42
        r = QRectF(m, 8, self.width() - 2 * m, self.height() - 26)
        p.setPen(QPen(QColor("#555")))
        p.drawRect(r)
        p.setPen(QPen(QColor("#aaa")))
        p.drawText(QRectF(r.left(), r.bottom() + 2, r.width(), 16), Qt.AlignCenter, "step")
        if not self.loss:
            p.drawText(r, Qt.AlignCenter, "Loss and accuracy appear here while training")
            return
        n = max(len(self.loss), 2)

        def path(vals, lo, hi, color, label, left):
            if hi <= lo:
                hi = lo + 1.0
            pts = [QPointF(r.left() + r.width() * i / (n - 1),
                           r.bottom() - r.height() * (v - lo) / (hi - lo)) for i, v in enumerate(vals)]
            p.setPen(QPen(QColor(color), 1.6))
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            p.setPen(QPen(QColor(color)))
            x = 2 if left else r.right() + 3
            p.drawText(QRectF(x, r.top() - 2, m - 4, 14), Qt.AlignLeft, f"{hi:.2g}")
            p.drawText(QRectF(x, r.bottom() - 12, m - 4, 14), Qt.AlignLeft, f"{lo:.2g}")
            p.drawText(QRectF(r.left() + 6 if left else r.right() - 122, r.top() + 4, 116, 14),
                       Qt.AlignLeft if left else Qt.AlignRight, label)

        finite = [v for v in self.loss if np.isfinite(v)]
        if finite:
            path(self.loss, 0.0, max(finite), "#4fc3f7", "loss", True)
        if self.metric_values:
            path(self.metric_values, 0.0, 1.0, "#ffb74d", self.metric_name, False)


class TrainWorker(QObject):
    """Builds the chip model and trains it, off the GUI thread."""

    built = Signal(object)  # dict: grid shape, box cells, run time
    step = Signal(object, object)  # (record dict, rho_phys array)
    finished = Signal(object, object)  # (TrainResult, ChipModel)
    failed = Signal(str)

    def __init__(self, document, config: ct.TrainConfig, task: ct.Task, options: ct.TrainOptions) -> None:
        super().__init__()
        self.document = document
        self.config = config
        self.task = task
        self.options = options
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True  # read by the training callback between steps

    def run(self) -> None:
        try:
            model = ct.ChipModel(self.document, self.config)
            self.built.emit({"grid": model.grid_shape, "box": model.shape, "run_fs": model.run_time_fs})

            def cb(rec, rho_phys):
                self.step.emit(rec, rho_phys)
                return self._stop

            result = ct.train_chip(model, self.task, self.options, callback=cb)
        except Exception as exc:  # surfaced in the tab, never kills the thread
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result, model)


class TrainTab(QWidget):
    """The Train tab. ``view`` is the layout canvas (for the design-box overlay,
    selection, and the undo stack placements go through)."""

    def __init__(self, document, view, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = document
        self.view = view
        self._ports: list[ct.Port] = []
        self._box_item = None
        self._thread: QThread | None = None
        self._worker: TrainWorker | None = None
        self._result: ct.TrainResult | None = None
        self._model: ct.ChipModel | None = None
        self._task: ct.Task | None = None

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)

        intro = QLabel(
            "Train a region of your layout like a neural network. Mark a design box, pick "
            "input and output ports, choose a task, and Train: the etch pattern inside the "
            "box is fitted by gradient descent, with one forward and one adjoint simulation "
            "per input port each step (2.5-D, on the GPU when JAX sees one). New here? "
            "Click “Insert priority-encoder demo”, then Train."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(intro)

        demo_row = QHBoxLayout()
        self.demo_button = QPushButton("Insert priority-encoder demo")
        self.demo_button.setToolTip(
            "Place N input stubs and N output stubs around an empty gap, then set the "
            "ports and the design box for you, ready to train a priority encoder."
        )
        self.demo_button.clicked.connect(self._on_insert_demo)
        demo_row.addWidget(self.demo_button)
        demo_row.addStretch(1)
        layout.addLayout(demo_row)

        layout.addWidget(self._build_box_group())
        layout.addWidget(self._build_ports_group())
        layout.addWidget(self._build_task_group())
        layout.addWidget(self._build_settings_group())

        buttons = QHBoxLayout()
        self.train_button = QPushButton("Train")
        self.train_button.clicked.connect(self._on_train)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop)
        self.place_button = QPushButton("Place design on layout")
        self.place_button.setEnabled(False)
        self.place_button.setToolTip(
            "Add the trained etch pattern to the layout as a 'trained_design' instance "
            "filling the design box (undoable; saved with the project and exported to GDS)."
        )
        self.place_button.clicked.connect(self._on_place)
        for b in (self.train_button, self.stop_button, self.place_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        live = QHBoxLayout()
        self.plot = CurvePlot()
        live.addWidget(self.plot, 3)
        self.design_label = QLabel("design")
        self.design_label.setAlignment(Qt.AlignCenter)
        self.design_label.setMinimumSize(180, 150)
        self.design_label.setStyleSheet("background: #1e1e1e; color: #888;")
        live.addWidget(self.design_label, 2)
        layout.addLayout(live)

        self.results_table = QTableWidget(0, 0)
        self.results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results_table.setMinimumHeight(160)
        layout.addWidget(self.results_table)

        self._on_task_changed(0)
        self.refresh_ports()

    # -- design box ------------------------------------------------------------

    def _build_box_group(self) -> QGroupBox:
        group = QGroupBox("Design box (µm)")
        form = QHBoxLayout(group)
        self.box_spins = []
        for label in ("left", "bottom", "right", "top"):
            spin = QDoubleSpinBox()
            spin.setRange(-1e6, 1e6)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.valueChanged.connect(self._update_box_overlay)
            form.addWidget(QLabel(label))
            form.addWidget(spin)
            self.box_spins.append(spin)
        sel = QPushButton("From selection")
        sel.setToolTip("Set the box to the bounding box of the components selected on the canvas.")
        sel.clicked.connect(self._on_box_from_selection)
        form.addWidget(sel)
        return group

    def box_um(self) -> tuple[float, float, float, float]:
        return tuple(s.value() for s in self.box_spins)

    def set_box_um(self, box) -> None:
        for spin, v in zip(self.box_spins, box):
            spin.blockSignals(True)
            spin.setValue(float(v))
            spin.blockSignals(False)
        self._update_box_overlay()

    def _on_box_from_selection(self) -> None:
        ids = [it.inst_id for it in self.view.scene().selectedItems()
               if hasattr(it, "inst_id") and it.inst_id in self.document.instances]
        if not ids:
            QMessageBox.information(self, "No selection", "Select one or more components on the canvas first.")
            return
        bbs = [self.document.instances[i].ref.dbbox() for i in ids]
        self.set_box_um((min(b.left for b in bbs), min(b.bottom for b in bbs),
                         max(b.right for b in bbs), max(b.top for b in bbs)))

    def _update_box_overlay(self) -> None:
        left, bottom, right, top = self.box_um()
        scene = self.view.scene()
        if self._box_item is None:
            pen = QPen(QColor(_BOX_COLOR), 0, Qt.DashLine)
            self._box_item = scene.addRect(QRectF(), pen, QBrush(QColor(255, 152, 0, 30)))
            self._box_item.setZValue(50)
        if right > left and top > bottom:
            self._box_item.setRect(QRectF(left, bottom, right - left, top - bottom))
            self._box_item.setVisible(True)
        else:
            self._box_item.setVisible(False)

    def clear_overlay(self) -> None:
        if self._box_item is not None:
            try:
                self.view.scene().removeItem(self._box_item)
            except RuntimeError:
                pass
            self._box_item = None

    # -- ports -----------------------------------------------------------------

    def _build_ports_group(self) -> QGroupBox:
        group = QGroupBox("Ports")
        v = QVBoxLayout(group)
        hint = QLabel(
            "Open (unrouted) waveguide ports in the layout. Mark the ones light enters as "
            "Input and the ones you detect as Output. Order matters: the first input listed "
            "is input 0, and so on (sorted left to right, then bottom to top)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 9pt;")
        v.addWidget(hint)
        self.ports_table = QTableWidget(0, 5)
        self.ports_table.setHorizontalHeaderLabels(["Port", "X (µm)", "Y (µm)", "Faces", "Role"])
        self.ports_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ports_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.ports_table.setMinimumHeight(130)
        v.addWidget(self.ports_table)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_ports)
        auto = QPushButton("Auto: left-facing in, right-facing out")
        auto.setToolTip("Mark every port facing left (-x) as an input and every port facing right (+x) as an output.")
        auto.clicked.connect(self._on_auto_roles)
        row.addWidget(refresh)
        row.addWidget(auto)
        row.addStretch(1)
        v.addLayout(row)
        return group

    def refresh_ports(self) -> None:
        """Re-list the open ports, keeping the roles of ports still present."""
        roles = {self.ports_table.item(r, 0).text(): self.ports_table.cellWidget(r, 4).currentIndex()
                 for r in range(self.ports_table.rowCount())}
        self._ports = sorted(ct.open_ports(self.document), key=lambda p: (round(p.x_um, 3), round(p.y_um, 3)))
        self.ports_table.setRowCount(len(self._ports))
        faces = {0: "+x", 90: "+y", 180: "−x", 270: "−y"}
        for r, p in enumerate(self._ports):
            self.ports_table.setItem(r, 0, QTableWidgetItem(p.name))
            self.ports_table.setItem(r, 1, QTableWidgetItem(f"{p.x_um:.3f}"))
            self.ports_table.setItem(r, 2, QTableWidgetItem(f"{p.y_um:.3f}"))
            self.ports_table.setItem(r, 3, QTableWidgetItem(faces.get(int(round(p.orientation_deg)) % 360,
                                                                     f"{p.orientation_deg:.0f}°")))
            combo = QComboBox()
            combo.addItems(_ROLES)
            combo.setCurrentIndex(roles.get(p.name, 0))
            self.ports_table.setCellWidget(r, 4, combo)

    def _on_auto_roles(self) -> None:
        for r, p in enumerate(self._ports):
            ang = int(round(p.orientation_deg)) % 360
            self.ports_table.cellWidget(r, 4).setCurrentIndex(1 if ang == 180 else 2 if ang == 0 else 0)

    def set_roles(self, inputs, outputs) -> None:
        names_in = {p.name for p in inputs}
        names_out = {p.name for p in outputs}
        for r, p in enumerate(self._ports):
            self.ports_table.cellWidget(r, 4).setCurrentIndex(
                1 if p.name in names_in else 2 if p.name in names_out else 0)

    def selected_ports(self) -> tuple[tuple[ct.Port, ...], tuple[ct.Port, ...]]:
        ins, outs = [], []
        for r, p in enumerate(self._ports):
            role = self.ports_table.cellWidget(r, 4).currentIndex()
            (ins if role == 1 else outs if role == 2 else []).append(p)
        return tuple(ins), tuple(outs)

    # -- task ------------------------------------------------------------------

    def _build_task_group(self) -> QGroupBox:
        group = QGroupBox("Task")
        v = QVBoxLayout(group)
        self.task_combo = QComboBox()
        for label, kind, _page in _TASKS:
            self.task_combo.addItem(label, kind)
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        v.addWidget(self.task_combo)
        self.task_stack = QStackedWidget()

        # 0: priority encoder
        pe = QWidget()
        f = QFormLayout(pe)
        self.pe_lines_spin = QSpinBox()
        self.pe_lines_spin.setRange(2, 8)
        self.pe_lines_spin.setValue(4)
        f.addRow("Lines", self.pe_lines_spin)
        self.pe_thermo_check = QCheckBox("Thermometer codes only (lines 0..k lit, as from a TDC delay line)")
        f.addRow("", self.pe_thermo_check)
        pe_note = QLabel(
            "Each input line is lit or dark (in phase, equal amplitude). The chip must put "
            "its light into output port k, where k is the highest-numbered lit line. Needs "
            "as many outputs as lines."
        )
        pe_note.setWordWrap(True)
        pe_note.setStyleSheet("color: gray; font-size: 9pt;")
        f.addRow(pe_note)
        self.task_stack.addWidget(pe)

        # 1: dataset file
        ds = QWidget()
        f = QFormLayout(ds)
        row = QHBoxLayout()
        self.data_path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse_data)
        row.addWidget(self.data_path_edit)
        row.addWidget(browse)
        f.addRow("File", row)
        self.data_note = QLabel()
        self.data_note.setWordWrap(True)
        self.data_note.setStyleSheet("color: gray; font-size: 9pt;")
        f.addRow(self.data_note)
        self.task_stack.addWidget(ds)

        # 2: target matrix
        tm = QWidget()
        f = QFormLayout(tm)
        self.matrix_edit = QPlainTextEdit(_MATRIX_PRESETS["2x2 50:50 coupler"])
        self.matrix_edit.setMaximumHeight(90)
        f.addRow("Matrix", self.matrix_edit)
        presets = QComboBox()
        presets.addItem("Presets…")
        presets.addItems(list(_MATRIX_PRESETS))
        presets.activated.connect(lambda i: i > 0 and self.matrix_edit.setPlainText(
            _MATRIX_PRESETS[presets.itemText(i)]))
        f.addRow("", presets)
        tm_note = QLabel(
            "Row m, column n is the amplitude from input n to output m. Entries are complex "
            "numbers (1, -1j, 0.5+0.5j), separated by spaces. Trained up to an overall scale "
            "and phase."
        )
        tm_note.setWordWrap(True)
        tm_note.setStyleSheet("color: gray; font-size: 9pt;")
        f.addRow(tm_note)
        self.task_stack.addWidget(tm)
        v.addWidget(self.task_stack)
        return group

    def _on_task_changed(self, idx: int) -> None:
        kind = _TASKS[idx][1]
        self.task_stack.setCurrentIndex(_TASKS[idx][2])
        if kind == "classify":
            self.data_note.setText(
                "NPZ with arrays X (n samples × N inputs, complex) and Y (n integer labels; "
                "class m is output port m), or CSV with columns in0, in1, … (complex amplitudes "
                "such as 1 or 0.5+0.5j) and label."
            )
        elif kind == "regress":
            self.data_note.setText(
                "NPZ with X (n × N, complex) and Y (n × M target powers), or CSV with columns "
                "in0, in1, … and out0, out1, …. Each target row is normalized to fractions of "
                "the detected power, and the chip is fitted to those fractions."
            )

    def _on_browse_data(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Dataset", "", "Datasets (*.npz *.csv);;All files (*)")
        if path:
            self.data_path_edit.setText(path)

    def current_task(self) -> ct.Task:
        kind = self.task_combo.currentData()
        if kind == "priority":
            return ct.priority_encoder_task(self.pe_lines_spin.value(), self.pe_thermo_check.isChecked())
        if kind == "transfer":
            return ct.Task("transfer", "target matrix", target_T=parse_matrix(self.matrix_edit.toPlainText()))
        path = self.data_path_edit.text().strip()
        if not path:
            raise ValueError("choose a dataset file")
        return ct.load_task_file(path, kind)

    # -- settings --------------------------------------------------------------

    def _build_settings_group(self) -> QGroupBox:
        group = QGroupBox("Training")
        f = QFormLayout(group)
        wl = self.document.project_settings.wavelength_um
        self.wavelength_spin = QDoubleSpinBox()
        self.wavelength_spin.setRange(0.2, 10.0)
        self.wavelength_spin.setDecimals(3)
        self.wavelength_spin.setValue(wl)
        f.addRow("Wavelength (µm)", self.wavelength_spin)
        self.cell_spin = QDoubleSpinBox()
        self.cell_spin.setRange(0.005, 1.0)
        self.cell_spin.setDecimals(4)
        self.cell_spin.setSingleStep(0.005)
        self.cell_spin.setValue(round(wl / 20, 4))
        self.cell_spin.setToolTip("Grid cell size. λ/20 is a good default; finer is more accurate and slower.")
        f.addRow("Cell size (µm)", self.cell_spin)
        self.feature_spin = QDoubleSpinBox()
        self.feature_spin.setRange(0.0, 2.0)
        self.feature_spin.setDecimals(3)
        self.feature_spin.setValue(0.1)
        self.feature_spin.setToolTip("Filter radius: roughly the smallest feature the trained etch can contain.")
        f.addRow("Min. feature (µm)", self.feature_spin)
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 5000)
        self.steps_spin.setValue(60)
        self.steps_spin.setToolTip("Optimizer steps. Each costs one forward and one adjoint simulation per input port.")
        f.addRow("Steps", self.steps_spin)
        self.lr_spin = QDoubleSpinBox()
        self.lr_spin.setRange(0.0001, 1.0)
        self.lr_spin.setDecimals(4)
        self.lr_spin.setValue(0.05)
        f.addRow("Learning rate", self.lr_spin)
        self.beta_spin = QDoubleSpinBox()
        self.beta_spin.setRange(1.0, 256.0)
        self.beta_spin.setValue(16.0)
        self.beta_spin.setToolTip(
            "Final projection sharpness. Training starts soft (2) and doubles towards this "
            "over the first three quarters of the steps, pushing the etch towards binary."
        )
        f.addRow("Final sharpness β", self.beta_spin)
        return group

    def current_config(self) -> ct.TrainConfig:
        inputs, outputs = self.selected_ports()
        return ct.TrainConfig(
            box_um=self.box_um(), inputs=inputs, outputs=outputs,
            wavelength_um=self.wavelength_spin.value(), cell_size_um=self.cell_spin.value(),
            filter_radius_um=self.feature_spin.value(),
        )

    def current_options(self) -> ct.TrainOptions:
        return ct.TrainOptions(steps=self.steps_spin.value(), learning_rate=self.lr_spin.value(),
                               beta_max=max(self.beta_spin.value(), 2.0))

    # -- demo ------------------------------------------------------------------

    def _on_insert_demo(self) -> None:
        from phidler.model.commands import AddInstanceCommand

        n = self.pe_lines_spin.value()
        # Beside whatever is already on the canvas, never on top of it.
        if self.document.instances:
            bb = self.document.top.dbbox()
            center = (bb.right + 8.0 + 4.0, (bb.top + bb.bottom) / 2.0)
        else:
            center = (0.0, 0.0)
        placements, box = ct.priority_encoder_stubs(n, center_um=center)
        stack = self.view.undo_stack
        scene = self.view.scene()
        placed = []
        if stack is not None:
            stack.beginMacro(f"Insert {n}-line priority-encoder demo")
        try:
            for role, line, spec, kwargs, x, y in placements:
                cmd = AddInstanceCommand(self.document, scene, spec, kwargs, x=x, y=y)
                if stack is not None:
                    stack.push(cmd)
                else:
                    cmd.redo()
                if cmd.error is not None:
                    raise cmd.error
                placed.append((role, line, cmd.inst_id))
        finally:
            if stack is not None:
                stack.endMacro()
        self.refresh_ports()
        inputs, outputs = ct.demo_ports(self.document, placed)
        self.set_roles(inputs, outputs)
        self.set_box_um(box)
        self.task_combo.setCurrentIndex(0)
        self.status_label.setText(
            f"Inserted a {n}-line demo: {n} inputs on the left, {n} outputs on the right, and the "
            "gap between them as the design box. Click Train."
        )
        try:
            self.view.fitInView(scene.itemsBoundingRect(), Qt.KeepAspectRatio)
        except Exception:
            pass

    # -- training --------------------------------------------------------------

    def is_training(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _on_train(self) -> None:
        try:
            task = self.current_task()
            config = self.current_config()
        except Exception as exc:
            QMessageBox.warning(self, "Can't train", str(exc))
            return
        if not config.inputs or not config.outputs:
            QMessageBox.warning(self, "Can't train", "Mark at least one input and one output port.")
            return
        if task.n_inputs != len(config.inputs):
            QMessageBox.warning(self, "Can't train", f"The task has {task.n_inputs} inputs but "
                                f"{len(config.inputs)} ports are marked Input.")
            return
        self._task = task
        self._result = None
        self.place_button.setEnabled(False)
        self.train_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.results_table.setRowCount(0)
        metric = {"classify": "accuracy", "regress": "1 − rms error", "transfer": "fidelity"}[task.kind]
        self.plot.clear(metric)
        self.status_label.setText("Building the port simulations…")

        self._thread = QThread(self)
        self._worker = TrainWorker(self.document, config, task, self.current_options())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.built.connect(self._on_built)
        self._worker.step.connect(self._on_step)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self.stop_button.setEnabled(False)
            self.status_label.setText("Stopping after this step…")

    def _on_built(self, info: dict) -> None:
        gx, gy, _gz = info["grid"]
        bx, by = info["box"]
        self.status_label.setText(
            f"Grid {gx}×{gy} (2.5-D), design box {bx}×{by} cells, {info['run_fs']:.0f} fs per run. "
            "Compiling the first step…"
        )

    @staticmethod
    def _metric(rec: dict) -> float | None:
        if "accuracy" in rec:
            return rec["accuracy"]
        if "fidelity" in rec:
            return rec["fidelity"]
        if "rms_error" in rec:
            return 1.0 - rec["rms_error"]
        return None

    def _on_step(self, rec: dict, rho_phys) -> None:
        self.plot.add(rec["value"], self._metric(rec))
        self._show_design(rho_phys)
        extra = ""
        if "accuracy" in rec:
            extra = f", accuracy {rec['accuracy']:.0%}"
        elif "fidelity" in rec:
            extra = f", fidelity {rec['fidelity']:.3f}"
        elif "rms_error" in rec:
            extra = f", rms error {rec['rms_error']:.3f}"
        self.status_label.setText(
            f"Step {rec['it'] + 1}/{self.steps_spin.value()}: loss {rec['value']:.4f}{extra}, "
            f"β = {rec['beta']:g} ({rec.get('time', 0.0):.1f} s/step)"
        )

    def _show_design(self, rho) -> None:
        img = _design_image(rho)
        pm = QPixmap.fromImage(img).scaled(self.design_label.size() * 0.95, Qt.KeepAspectRatio, Qt.FastTransformation)
        self.design_label.setPixmap(pm)

    def _on_finished(self, result: ct.TrainResult, model: ct.ChipModel) -> None:
        self._result, self._model = result, model
        self.train_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.place_button.setEnabled(True)
        self._show_design(result.rho_phys)
        import photonfdtd as pf

        m = result.metrics
        head = "Stopped" if result.stopped else "Done"
        if "accuracy" in m:
            score = f"accuracy {m['accuracy']:.0%}"
        elif "fidelity" in m:
            score = f"fidelity {m['fidelity']:.3f}"
        else:
            score = f"rms error {m['rms_error']:.3f}"
        self.status_label.setText(
            f"{head} after {len(result.history)} steps in {result.elapsed_s:.0f} s: {score}, "
            f"binarization {pf.binarization(result.rho_phys):.2f} (1 = fully etched/unetched). "
            "Place the design on the layout to keep it."
        )
        self._fill_results(result)

    def _fill_results(self, result: ct.TrainResult) -> None:
        task, t = self._task, self.results_table
        if task.kind == "transfer":
            T = np.abs(result.T) ** 2
            T = T / T.max()
            t.setRowCount(T.shape[0])
            t.setColumnCount(T.shape[1])
            t.setHorizontalHeaderLabels([f"from in {n}" for n in range(T.shape[1])])
            t.setVerticalHeaderLabels([f"to out {m}" for m in range(T.shape[0])])
            for i in range(T.shape[0]):
                for j in range(T.shape[1]):
                    t.setItem(i, j, QTableWidgetItem(f"{T[i, j]:.3f}"))
            return
        frac = result.metrics["fractions"]
        M = frac.shape[1]
        t.setRowCount(len(frac))
        t.setColumnCount(3 + M)
        t.setHorizontalHeaderLabels(["Input", "Target", "Predicted"] + [f"P{m} share" for m in range(M)])
        t.verticalHeader().setVisible(False)
        for r, (x, f) in enumerate(zip(task.X, frac)):
            if np.allclose(x.imag, 0) and np.all(np.isin(x.real, (0, 1))):
                xs = "".join(str(int(v)) for v in x.real)
            else:
                xs = " ".join(f"{v:.2g}" for v in x)
            pred = int(np.argmax(f))
            if task.kind == "classify":
                target = str(int(task.Y[r]))
                ok = pred == int(task.Y[r])
            else:
                target = " ".join(f"{v:.2f}" for v in task.Y[r])
                ok = None
            t.setItem(r, 0, QTableWidgetItem(xs))
            t.setItem(r, 1, QTableWidgetItem(target))
            item = QTableWidgetItem(str(pred))
            if ok is not None:
                item.setForeground(QColor("#2e7d32" if ok else "#c62828"))
            t.setItem(r, 2, item)
            for m in range(M):
                t.setItem(r, 3 + m, QTableWidgetItem(f"{f[m]:.2f}"))

    def _on_failed(self, message: str) -> None:
        self.train_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText(f"Error: {message}")

    def _on_place(self) -> None:
        from phidler.model.commands import AddInstanceCommand

        if self._result is None or self._model is None:
            return
        ct.register_trained_cell()
        left, bottom, right, top = self._model.config.box_um
        polys = ct.design_polygons(self._model, self._result.rho_phys)
        kwargs = {"polygons": [[list(pt) for pt in loop] for loop in polys],
                  "width": round(right - left, 4), "height": round(top - bottom, 4)}
        cmd = AddInstanceCommand(self.document, self.view.scene(), ct.TRAINED_CELL, kwargs,
                                 x=(left + right) / 2.0, y=(bottom + top) / 2.0,
                                 text="Place trained design")
        if self.view.undo_stack is not None:
            self.view.undo_stack.push(cmd)
        else:
            cmd.redo()
        if cmd.error is not None:
            if self.view.undo_stack is not None:
                self.view.undo_stack.undo()
            QMessageBox.warning(self, "Couldn't place the design", str(cmd.error))
            return
        self.status_label.setText(
            f"Placed the trained design as instance #{cmd.inst_id}. It's saved with the project and "
            "exported with the layout's GDS."
        )

    def shutdown(self) -> None:
        """Called when the FDTD window closes: stop a run and drop the overlay."""
        if self.is_training():
            self._worker.request_stop()
            for sig in (self._worker.finished, self._worker.failed, self._worker.step):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
            self._thread.quit()
            self._thread.wait()
        self.clear_overlay()
