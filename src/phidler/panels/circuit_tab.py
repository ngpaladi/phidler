"""The FDTD window's Circuit tab: simulate the whole chip as a circuit.

The layout is too big for FDTD, so this tab hands it to photonfdtd.circuit
(see phidler.chip_circuit): every placed component gets a physics-based model,
the netlist joins them, and the metal from each pad is followed to the
electrodes, heaters and detectors it reaches. The user sets the laser inputs,
a drive per net (DC, square wave, sine, pulse, PRBS) and runs an operating
point, a voltage sweep, a wavelength sweep or a time-domain run; photocurrents
and output powers are plotted and tabulated.

The passive components' models can also be measured by FDTD, one component at
a time (see phidler.component_sim): each distinct part is simulated alone on a
small grid and the circuit joins the results, so the memory needed is that of
the largest single part instead of the whole layout. Modulators and heaters
are run the same way at the voltages the user lists (off and on, say).
"""
from __future__ import annotations

import threading

import numpy as np
from PySide6.QtCore import QCoreApplication, QObject, QPointF, QRectF, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from phidler import chip_circuit as cc

_COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176", "#90a4ae", "#f06292"]


class SeriesPlot(QWidget):
    """Several named curves against one x axis, with a legend."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.x = np.zeros(0)
        self.series: dict[str, np.ndarray] = {}
        self.x_label = ""
        self.y_label = ""
        self.setMinimumHeight(220)

    def set_data(self, x, series: dict, x_label: str, y_label: str) -> None:
        self.x = np.asarray(x, float)
        self.series = {k: np.asarray(v, float) for k, v in series.items()}
        self.x_label, self.y_label = x_label, y_label
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        ml, mb = 56, 30
        r = QRectF(ml, 10, self.width() - ml - 12, self.height() - mb - 14)
        p.setPen(QPen(QColor("#555")))
        p.drawRect(r)
        p.setPen(QPen(QColor("#aaa")))
        p.drawText(QRectF(r.left(), r.bottom() + 12, r.width(), 16), Qt.AlignCenter, self.x_label)
        if not self.series or self.x.size < 2:
            p.drawText(r, Qt.AlignCenter, "Results appear here")
            return
        ys = np.concatenate([v[np.isfinite(v)] for v in self.series.values()] or [np.zeros(1)])
        lo, hi = (float(ys.min()), float(ys.max())) if ys.size else (0.0, 1.0)
        if hi - lo < 1e-12:
            lo, hi = lo - 0.5, hi + 0.5
        pad = 0.05 * (hi - lo)
        lo, hi = lo - pad, hi + pad
        x0, x1 = float(self.x.min()), float(self.x.max())
        if x1 <= x0:
            x1 = x0 + 1.0

        def pt(x, y):
            return QPointF(r.left() + r.width() * (x - x0) / (x1 - x0),
                           r.bottom() - r.height() * (y - lo) / (hi - lo))

        p.drawText(QRectF(2, r.top() - 4, ml - 6, 14), Qt.AlignRight, f"{hi:.3g}")
        p.drawText(QRectF(2, r.bottom() - 10, ml - 6, 14), Qt.AlignRight, f"{lo:.3g}")
        p.drawText(QRectF(r.left(), r.bottom() + 1, 80, 14), Qt.AlignLeft, f"{x0:.4g}")
        p.drawText(QRectF(r.right() - 80, r.bottom() + 1, 80, 14), Qt.AlignRight, f"{x1:.4g}")
        p.save()
        p.translate(12, r.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-r.height() / 2, -8, r.height(), 16), Qt.AlignCenter, self.y_label)
        p.restore()
        for k, (name, y) in enumerate(self.series.items()):
            color = QColor(_COLORS[k % len(_COLORS)])
            p.setPen(QPen(color, 1.6))
            pts = [pt(x, v) for x, v in zip(self.x, y) if np.isfinite(v)]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            p.drawText(QRectF(r.left() + 8, r.top() + 4 + 15 * k, r.width() - 16, 14),
                       Qt.AlignLeft, name)


#: Worker threads still finishing after their tab closed, kept referenced
#: until they have stopped.
_ORPHANS: set = set()


class _Reaper(QObject):
    """Lives in the GUI thread. A closed tab's worker thread signals it when
    it ends; the slot runs here (a queued call), after the thread has left
    its run loop, and only then drops the last reference."""

    @Slot()
    def reap(self) -> None:
        for pair in [p for p in _ORPHANS if p[0].isFinished()]:
            pair[0].wait()
            _ORPHANS.discard(pair)

    def adopt(self, thread: QThread, worker: QObject) -> None:
        _ORPHANS.add((thread, worker))
        thread.finished.connect(self.reap, Qt.QueuedConnection)

    def wait_all(self) -> None:
        for t, _ in list(_ORPHANS):
            t.wait()
        _ORPHANS.clear()


_REAPER: _Reaper | None = None


def _reaper() -> _Reaper:
    global _REAPER
    if _REAPER is None:
        _REAPER = _Reaper()
        app = QCoreApplication.instance()
        if app is not None:
            _REAPER.moveToThread(app.thread())
            app.aboutToQuit.connect(_REAPER.wait_all)
    return _REAPER


class CircuitWorker(QObject):
    """Builds the circuit, or runs an analysis on it, off the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self) -> None:
        try:
            out = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # surfaced in the tab
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(out)


class CircuitTab(QWidget):
    """The Circuit tab, simulating ``document``'s whole layout."""

    ANALYSES = (("op", "Operating point"), ("vsweep", "Voltage sweep"),
                ("wl", "Wavelength sweep"), ("time", "Time domain"))

    def __init__(self, document, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = document
        self.circuit: cc.ChipCircuit | None = None
        self.result = None
        self._thread: QThread | None = None
        self._worker: CircuitWorker | None = None
        self._cancel = threading.Event()

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)

        intro = QLabel(
            "Simulate the whole chip as a circuit. Every placed component gets a physics model "
            "(lithium niobate switches from their electrode cross-sections, waveguides and routes "
            "from their length, photodetectors from their responsivity); the metal from each pad "
            "is followed to the electrodes, heaters and detectors it reaches. Place parts from "
            "“Lithium Niobate (photonfdtd)” and pads, wire them with metal routes, then Build."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(intro)
        if not cc.AVAILABLE:
            warn = QLabel(cc.UNAVAILABLE_REASON)
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #e57373;")
            layout.addWidget(warn)

        layout.addWidget(self._build_settings_group())
        layout.addWidget(self._build_fdtd_group())
        build_row = QHBoxLayout()
        self.build_button = QPushButton("Build circuit from layout")
        self.build_button.setEnabled(cc.AVAILABLE)
        self.build_button.setToolTip(
            "Read the layout's netlist and model every component. The first time a lithium "
            "niobate design is seen its cross-section is solved (about a minute); after that it "
            "comes from the cache.")
        self.build_button.clicked.connect(self._on_build)
        build_row.addWidget(self.build_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip("Stop a component-by-component build after the FDTD run "
                                    "in progress. Components already measured stay cached.")
        self.stop_button.clicked.connect(self._on_stop)
        build_row.addWidget(self.stop_button)
        build_row.addStretch(1)
        layout.addLayout(build_row)

        self.components_table = QTableWidget(0, 2)
        self.components_table.setHorizontalHeaderLabels(["Component", "Model"])
        self.components_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.components_table.setMaximumHeight(150)
        layout.addWidget(QLabel("Components"))
        layout.addWidget(self.components_table)

        self.inputs_table = QTableWidget(0, 2)
        self.inputs_table.setHorizontalHeaderLabels(["Optical port", "Laser power (mW)"])
        self.inputs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.inputs_table.setMaximumHeight(130)
        layout.addWidget(QLabel("Optical inputs"))
        layout.addWidget(self.inputs_table)

        self.drives_table = QTableWidget(0, 6)
        self.drives_table.setHorizontalHeaderLabels(
            ["Net", "Connects", "Drive", "Low / DC (V)", "High (V)", "Freq / rate (Hz)"])
        self.drives_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.drives_table.setMaximumHeight(200)
        layout.addWidget(QLabel("Electrical drives (per net)"))
        layout.addWidget(self.drives_table)

        layout.addWidget(self._build_analysis_group())
        run_row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self._on_run)
        run_row.addWidget(self.run_button)
        run_row.addStretch(1)
        layout.addLayout(run_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.current_plot = SeriesPlot()
        self.power_plot = SeriesPlot()
        layout.addWidget(self.current_plot)
        layout.addWidget(self.power_plot)
        self.results_table = QTableWidget(0, 2)
        self.results_table.setHorizontalHeaderLabels(["Quantity", "Value"])
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.results_table)
        layout.addStretch(1)

    # ------------------------------------------------------------------ #
    def _spin(self, lo, hi, val, decimals=3, step=0.1, suffix=""):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        return s

    def _build_settings_group(self) -> QGroupBox:
        box = QGroupBox("Circuit")
        form = QFormLayout(box)
        c = cc.CircuitConfig()
        self.wl_spin = self._spin(0.4, 5.0, c.wavelength, 4, 0.01, " um")
        self.neff_spin = self._spin(1.0, 4.0, c.neff, 3, 0.01)
        self.ng_spin = self._spin(1.0, 5.0, c.ng, 3, 0.01)
        self.loss_spin = self._spin(0.0, 100.0, c.loss_db_cm, 2, 0.1, " dB/cm")
        self.bw_spin = self._spin(0.0, 1000.0, c.bandwidth_ghz, 1, 1.0, " GHz")
        form.addRow("Wavelength", self.wl_spin)
        form.addRow("Waveguide n_eff", self.neff_spin)
        form.addRow("Waveguide n_g", self.ng_spin)
        form.addRow("Waveguide loss", self.loss_spin)
        form.addRow("Modulator bandwidth", self.bw_spin)
        return box

    def _build_fdtd_group(self) -> QGroupBox:
        from phidler import component_sim as cs

        d = cs.ComponentSimConfig()
        box = QGroupBox("Measure components by FDTD, one component at a time")
        box.setCheckable(True)
        box.setChecked(False)
        box.setToolTip(
            "Instead of ideal models, simulate each distinct passive component (bend, taper, "
            "splitter, crossing, ring, …) alone on its own small FDTD grid, and join the "
            "measured S-matrices along the layout's connections. Memory is then set by the "
            "largest single component, not the whole layout, and a component used many times "
            "is simulated once. Results are cached, so only new or changed components run.")
        self.fdtd_group = box
        form = QFormLayout(box)
        note = QLabel(
            "Each distinct passive part is simulated alone and the circuit joins the results, "
            "so the whole layout never has to fit in memory at once. Straights keep their "
            "analytic model; modulators and heaters keep photonfdtd's unless voltages are "
            "given for them below. Parts are assumed to couple only through their ports.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 9pt;")
        form.addRow(note)
        self.fdtd_domain_combo = QComboBox()
        self.fdtd_domain_combo.addItem("2.5-D (effective index), fast", True)
        self.fdtd_domain_combo.addItem("Full 3-D", False)
        form.addRow("Domain", self.fdtd_domain_combo)
        self.fdtd_scope_combo = QComboBox()
        self.fdtd_scope_combo.addItem("Every passive component", False)
        self.fdtd_scope_combo.addItem("Only components with no built-in model", True)
        form.addRow("Simulate", self.fdtd_scope_combo)
        self.fdtd_cell_spin = self._spin(0.0, 1.0, 0.0, 4, 0.005, " um")
        self.fdtd_cell_spin.setSpecialValueText("Auto (wavelength / 30, or / 20 in 3-D)")
        form.addRow("Cell size", self.fdtd_cell_spin)
        self.fdtd_span_spin = self._spin(0.0, 2.0, d.span_um, 4, 0.01, " um")
        self.fdtd_span_spin.setToolTip(
            "Width of the wavelength band the S-matrices are recorded over, centred on the "
            "circuit's wavelength. One pulse covers the band. A wavelength sweep outside it "
            "holds the values at the band's edge.")
        form.addRow("Band width", self.fdtd_span_spin)
        self.fdtd_points_spin = QSpinBox()
        self.fdtd_points_spin.setRange(1, 201)
        self.fdtd_points_spin.setValue(d.n_wavelengths)
        form.addRow("Wavelengths in band", self.fdtd_points_spin)
        self.fdtd_max_cells_spin = self._spin(0.1, 100000.0, d.max_cells / 1e6, 1, 5.0, " M cells")
        self.fdtd_max_cells_spin.setToolTip(
            "The largest grid one component may use, about 100 MB per million cells. A "
            "larger bend or taper keeps its analytic model; any other component stops the "
            "build with a message.")
        form.addRow("Largest component grid", self.fdtd_max_cells_spin)
        self.fdtd_drive_edit = QLineEdit()
        self.fdtd_drive_edit.setPlaceholderText(
            "for example 0, 5 (off and on); empty: keep photonfdtd's models")
        self.fdtd_drive_edit.setToolTip(
            "Voltages to simulate the modulators and heaters at, separated by commas. Each "
            "one is run by FDTD once per voltage: on a modulator's signal electrode with its "
            "grounds at 0 V, or across a heater. The index change each volt causes is taken "
            "from photonfdtd's model of the part and applied to the waveguide under its "
            "electrodes. The circuit is exact at these voltages; a drive between two of them "
            "is interpolated and one outside them is held at the nearest, so list every "
            "state you want to see (off and on, or the points of a sweep).")
        form.addRow("Driven parts at (V)", self.fdtd_drive_edit)
        self.fdtd_cache_check = QCheckBox("Reuse cached results")
        self.fdtd_cache_check.setChecked(True)
        form.addRow("", self.fdtd_cache_check)
        return box

    def fdtd_config(self):
        """The per-component FDTD settings, or None when the group is off.
        Raises ValueError when the driven parts' voltages do not parse."""
        from phidler import component_sim as cs

        if not self.fdtd_group.isChecked():
            return None
        text = self.fdtd_drive_edit.text().replace(";", ",")
        try:
            volts = tuple(float(v) for v in text.split(",") if v.strip())
        except ValueError:
            raise ValueError(f"The driven parts' voltages are not numbers: {text.strip()!r}. "
                             "Give them separated by commas, for example 0, 5.") from None
        return cs.ComponentSimConfig(
            wavelength_um=self.wl_spin.value(),
            span_um=self.fdtd_span_spin.value(),
            n_wavelengths=self.fdtd_points_spin.value(),
            cell_size_um=self.fdtd_cell_spin.value() or None,
            eim=bool(self.fdtd_domain_combo.currentData()),
            max_cells=int(self.fdtd_max_cells_spin.value() * 1e6),
            only_unmodelled=bool(self.fdtd_scope_combo.currentData()),
            use_cache=self.fdtd_cache_check.isChecked(),
            drive_voltages=volts,
        )

    def _build_analysis_group(self) -> QGroupBox:
        box = QGroupBox("Analysis")
        form = QFormLayout(box)
        self.analysis_combo = QComboBox()
        for key, label in self.ANALYSES:
            self.analysis_combo.addItem(label, key)
        form.addRow("Run", self.analysis_combo)
        self.sweep_net_combo = QComboBox()
        self.sweep_from = self._spin(-1000, 1000, -10.0, 3, 1.0, " V")
        self.sweep_to = self._spin(-1000, 1000, 10.0, 3, 1.0, " V")
        self.sweep_points = QSpinBox()
        self.sweep_points.setRange(2, 5001)
        self.sweep_points.setValue(81)
        form.addRow("Sweep net", self.sweep_net_combo)
        form.addRow("Sweep from", self.sweep_from)
        form.addRow("Sweep to", self.sweep_to)
        form.addRow("Points", self.sweep_points)
        self.wl_from = self._spin(0.4, 5.0, 1.50, 4, 0.01, " um")
        self.wl_to = self._spin(0.4, 5.0, 1.60, 4, 0.01, " um")
        form.addRow("Wavelength from", self.wl_from)
        form.addRow("Wavelength to", self.wl_to)
        self.t_stop = self._spin(0.001, 1e6, 5.0, 3, 1.0, " ns")
        self.t_steps = QSpinBox()
        self.t_steps.setRange(10, 200000)
        self.t_steps.setValue(1000)
        form.addRow("Duration", self.t_stop)
        form.addRow("Time steps", self.t_steps)
        return box

    def config(self) -> cc.CircuitConfig:
        return cc.CircuitConfig(wavelength=self.wl_spin.value(), neff=self.neff_spin.value(),
                                ng=self.ng_spin.value(), loss_db_cm=self.loss_spin.value(),
                                bandwidth_ghz=self.bw_spin.value())

    # ------------------------------------------------------------------ #
    def is_busy(self) -> bool:
        return self._thread is not None

    def _start(self, fn, args, done, with_progress: bool = False) -> None:
        self.build_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self._cancel.clear()
        self._thread = QThread(self)
        self._worker = CircuitWorker(fn, *args)
        if with_progress:
            # The job reports through the worker's signal (queued to this
            # thread) and polls the Stop button's flag between FDTD runs.
            self._worker.kwargs = dict(progress=self._worker.progress.emit,
                                       cancel=self._cancel.is_set)
            self._worker.progress.connect(self.status_label.setText)
            self.stop_button.setEnabled(True)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_done)
        self._thread.start()

    def _on_thread_done(self) -> None:
        for obj in (self._worker, self._thread):
            if obj is not None:
                obj.deleteLater()
        self._thread = None
        self._worker = None
        self.stop_button.setEnabled(False)
        self.build_button.setEnabled(cc.AVAILABLE)
        self.run_button.setEnabled(self.circuit is not None)

    def _on_stop(self) -> None:
        self._cancel.set()
        self.stop_button.setEnabled(False)
        self.status_label.setText("Stopping after the FDTD run in progress…")

    def _on_failed(self, msg: str) -> None:
        self.status_label.setText(msg)

    def _on_build(self) -> None:
        if not self.document.instances:
            self.status_label.setText("The layout is empty.")
            return
        self.status_label.setText("Building the circuit (lithium niobate cross-sections are "
                                  "solved the first time a design is seen)…")
        try:
            fdtd = self.fdtd_config()
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        if fdtd is None:
            self._start(cc.build_circuit, (self.document, self.config()), self._on_built)
            return
        self.status_label.setText("Measuring the components by FDTD, one at a time…")
        self._start(cc.build_circuit, (self.document, self.config(), fdtd), self._on_built,
                    with_progress=True)

    def _on_built(self, circ: cc.ChipCircuit) -> None:
        self.circuit = circ
        chip = circ.chip
        rows = [(circ.label(n), circ.model_kind(n)) for n in chip.models]
        self.components_table.setRowCount(len(rows))
        for i, (a, b) in enumerate(rows):
            self.components_table.setItem(i, 0, QTableWidgetItem(a))
            self.components_table.setItem(i, 1, QTableWidgetItem(b))
        ports = circ.optical_ports
        self.inputs_table.setRowCount(len(ports))
        for i, p in enumerate(ports):
            self.inputs_table.setItem(i, 0, QTableWidgetItem(self._port_label(p)))
            self.inputs_table.item(i, 0).setData(Qt.UserRole, p)
            self.inputs_table.item(i, 0).setFlags(Qt.ItemIsEnabled)
            spin = self._spin(0.0, 1000.0, 1.0 if i == 0 else 0.0, 3, 0.1)
            self.inputs_table.setCellWidget(i, 1, spin)
        nets = circ.nets
        self.drives_table.setRowCount(len(nets))
        self.sweep_net_combo.clear()
        for i, net in enumerate(nets):
            item = QTableWidgetItem(net)
            item.setFlags(Qt.ItemIsEnabled)
            self.drives_table.setItem(i, 0, item)
            conn = QTableWidgetItem(", ".join(circ.net_members.get(net, [])))
            conn.setFlags(Qt.ItemIsEnabled)
            self.drives_table.setItem(i, 1, conn)
            kind = QComboBox()
            for k in cc.DRIVE_KINDS:
                kind.addItem(k.upper() if k in ("dc", "prbs") else k.capitalize(), k)
            kind.setCurrentIndex(kind.findData("dc"))
            self.drives_table.setCellWidget(i, 2, kind)
            self.drives_table.setCellWidget(i, 3, self._spin(-1e4, 1e4, 0.0, 3, 0.5))
            self.drives_table.setCellWidget(i, 4, self._spin(-1e4, 1e4, 1.0, 3, 0.5))
            freq = QLineEdit("1e9")
            self.drives_table.setCellWidget(i, 5, freq)
            self.sweep_net_combo.addItem(net)
        n_det = len(circ.detectors)
        self.status_label.setText(
            f"{len(chip.models)} components, {len(ports)} optical ports, {len(nets)} nets, "
            f"{n_det} photodetector{'s' * (n_det != 1)}. Set the drives and Run.")
        if circ.fdtd is not None:
            extra = [circ.fdtd.summary()] + [f"Note: {n}" for n in circ.fdtd.notes()]
            self.status_label.setText(self.status_label.text() + "\n" + "\n".join(extra))

    def _port_label(self, port: str) -> str:
        if self.circuit is None or "," not in port:
            return port
        inst, p = port.split(",", 1)
        return f"{self.circuit.label(inst)} : {p}"

    def drive_specs(self) -> dict[str, cc.DriveSpec]:
        specs = {}
        for i in range(self.drives_table.rowCount()):
            net = self.drives_table.item(i, 0).text()
            kind = self.drives_table.cellWidget(i, 2).currentData()
            try:
                freq = float(self.drives_table.cellWidget(i, 5).text())
            except ValueError:
                freq = 1e9
            specs[net] = cc.DriveSpec(kind, self.drives_table.cellWidget(i, 3).value(),
                                      self.drives_table.cellWidget(i, 4).value(), freq)
        return specs

    def set_drive(self, net: str, kind: str, low: float = 0.0, high: float = 1.0,
                  freq: float = 1e9) -> None:
        """Set one net's drive (also used by scripts and tests)."""
        for i in range(self.drives_table.rowCount()):
            if self.drives_table.item(i, 0).text() == net:
                combo = self.drives_table.cellWidget(i, 2)
                combo.setCurrentIndex(combo.findData(kind))
                self.drives_table.cellWidget(i, 3).setValue(low)
                self.drives_table.cellWidget(i, 4).setValue(high)
                self.drives_table.cellWidget(i, 5).setText(repr(float(freq)))
                return
        raise KeyError(net)

    def inputs_mw(self) -> dict[str, float]:
        return {self.inputs_table.item(i, 0).data(Qt.UserRole): self.inputs_table.cellWidget(i, 1).value()
                for i in range(self.inputs_table.rowCount())}

    def _on_run(self) -> None:
        if self.circuit is None:
            return
        kind = self.analysis_combo.currentData()
        specs, inputs, wl = self.drive_specs(), self.inputs_mw(), self.wl_spin.value()
        if kind == "op":
            args = (cc.operating_point, (self.circuit, specs, inputs, wl))
        elif kind == "vsweep":
            values = np.linspace(self.sweep_from.value(), self.sweep_to.value(),
                                 self.sweep_points.value())
            args = (cc.voltage_sweep, (self.circuit, specs, inputs, wl,
                                       self.sweep_net_combo.currentText(), values))
        elif kind == "wl":
            wls = np.linspace(self.wl_from.value(), self.wl_to.value(), self.sweep_points.value())
            args = (cc.wavelength_sweep, (self.circuit, specs, inputs, wls))
        else:
            t = np.linspace(0.0, self.t_stop.value() * 1e-9, self.t_steps.value())
            args = (cc.time_domain, (self.circuit, specs, inputs, wl, t))
        self.status_label.setText("Running…")
        self._start(args[0], args[1], self._on_result)

    def _on_result(self, res) -> None:
        self.result = res
        rows = []
        if isinstance(res, cc.Curves):
            self.current_plot.set_data(res.x, res.currents, res.x_label, "photocurrent (mA)")
            self.power_plot.set_data(res.x, {self._port_label(k): v for k, v in res.powers.items()},
                                     res.x_label, "power out (mW)")
            self.status_label.setText(f"Done: {len(res.x)} points.")
            for k, v in res.currents.items():
                rows.append((f"{k} photocurrent, mean (mA)", f"{np.mean(v):.4g}"))
        else:   # an operating point: a ChipState
            circ = self.circuit
            for d in circ.detectors:
                rows.append((f"{circ.label(d)} photocurrent (mA)",
                             f"{res.detector_current[d][0] * 1e3:.4g}"))
            for p in circ.optical_ports:
                rows.append((f"{self._port_label(p)} power out (mW)", f"{res.power[p][0] * 1e3:.4g}"))
            for n, v in res.voltage.items():
                rows.append((f"{n} voltage (V)", f"{v[0]:.4g}"))
            for n, v in res.heater_power.items():
                rows.append((f"{circ.label(n)} heater power (mW)", f"{v[0] * 1e3:.4g}"))
            self.status_label.setText("Done: operating point.")
        notes = getattr(self.circuit, "notes", [])
        if notes:
            self.status_label.setText(self.status_label.text() + "\nNote: " + "\nNote: ".join(notes))
        self.results_table.setRowCount(len(rows))
        for i, (a, b) in enumerate(rows):
            self.results_table.setItem(i, 0, QTableWidgetItem(a))
            self.results_table.setItem(i, 1, QTableWidgetItem(b))

    def shutdown(self) -> None:
        """Called when the FDTD window closes. A build or run cannot be
        interrupted (an LN solve can take a minute): its results are
        dropped, and its thread is kept alive, unparented, until it ends."""
        thread, worker = self._thread, self._worker
        self._cancel.set()  # a component-by-component build stops after its current run
        if thread is None:
            return
        for sig in (worker.finished, worker.failed, thread.finished):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._thread = self._worker = None
        thread.quit()  # ends the thread's event loop once the current job returns
        if thread.wait(2000):
            self._usable_again()
            return
        # Still working: the window is reused when it reopens, so the tab
        # comes back to life when that job ends, and not before (two builds
        # must not read the layout at once).
        self.status_label.setText("Stopped: the window was closed. The interrupted "
                                  "job is still finishing…")
        thread.finished.connect(self._usable_again)
        thread.setParent(None)
        _reaper().adopt(thread, worker)

    def _usable_again(self) -> None:
        if self._thread is None:
            self.stop_button.setEnabled(False)
            self.build_button.setEnabled(cc.AVAILABLE)
            self.run_button.setEnabled(self.circuit is not None)
            self.status_label.setText("Stopped: the window was closed.")
