from __future__ import annotations

import inspect
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from phidler.pdk_catalog import list_cross_section_names

# Parameter names that take a value from a fixed PDK-defined vocabulary,
# mapped to the function that lists the valid options. Using a dropdown
# here prevents the invalid-edit path entirely (typo'd cross_section names
# used to reach gf.get_component and raise) instead of just catching it.
_VOCABULARY_FIELDS: dict[str, Callable[[], list[str]]] = {
    "cross_section": list_cross_section_names,
}

# Numeric parameters that read as length-ish or numeric but carry NO unit, so
# the unit inference below must not slap "µm" on them: p is the euler-bend
# fraction (0–1), neff/nclad/ncore are refractive indices.
_DIMENSIONLESS_PARAMS = {"p", "neff", "nclad", "ncore"}

# Substrings that mark a value in microns. gdsfactory's geometric parameters
# are overwhelmingly in µm; angles (handled first) are the main exception.
_MICRON_TOKENS = (
    "length", "width", "gap", "radius", "size", "spacing", "pitch", "offset",
    "distance", "taper", "height", "thickness", "wavelength", "period",
    "extension", "enclosure", "margin", "dx", "dy",
)


def _unit_for_param(name: str, value: object) -> str:
    """The display unit for a component parameter, or "" when none applies.

    gdsfactory uses µm for geometry and degrees for angles; counts, indices,
    and fractions are unitless. Conservative by name + value type so an unknown
    parameter just gets no unit rather than a wrong one."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    if name in _DIMENSIONLESS_PARAMS:
        return ""
    if name.startswith(("n_", "num")):  # counts: n_periods, n_turns, num_pts, …
        return ""
    if "angle" in name or "orientation" in name:
        return "°"
    if any(token in name for token in _MICRON_TOKENS):
        return "µm"
    return ""


class PropertiesPanel(QWidget):
    """Dynamic parameter form for the selected instance, built from the
    component factory's signature. Non-scalar parameters (ComponentSpec,
    CrossSectionSpec callables, etc.) are shown read-only rather than
    guessed at — editing those is a stretch goal, not a v1 blocker."""

    params_applied = Signal(int, dict)
    # inst_id, x, y, rotation_deg, mirror, mag — precision/typed alternative
    # to dragging on the canvas, for exact placement (e.g. matching a known
    # coordinate from a foundry PDK or an existing layout) rather than
    # eyeballing a drag. Pushes the same MoveInstanceCommand drag/handle
    # gestures do, so it's undoable the same way.
    transform_applied = Signal(int, float, float, float, bool, float)
    # inst_id, columns, rows, column_pitch, row_pitch — tile the instance into
    # a rectangular array (replaces the old standalone *_array components).
    array_applied = Signal(int, int, int, float, float)
    # route_id, target value, unit ("µm"/"fs"/"ns"), auto — change a placed
    # route's length goal after the fact. value<=0 clears the goal (routes it
    # directly). The window converts the value+unit to µm and re-routes.
    route_length_applied = Signal(int, float, str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._inst_id: int | None = None
        self._route_id: int | None = None
        self._fields: dict[str, QWidget] = {}
        self._bbox_extent: tuple[float, float] = (0.0, 0.0)

        # All the form content lives inside a scroll area. Without it, the
        # panel's *minimum* size hint grows with the selected component's
        # parameter count (Transform + Array groups + N param rows ≈ 780px for
        # an MMI), and since a dock can't shrink below its minimum, selecting a
        # component forced QMainWindow to reflow — stealing height from the
        # bottom Console dock (it vanished) and resizing the canvas (placement
        # appeared to jump). Scrolling keeps the panel's minimum small.
        content = QWidget()
        layout = QVBoxLayout(content)
        self.title_label = QLabel("No selection")
        layout.addWidget(self.title_label)

        self.transform_group = QGroupBox("Transform")
        transform_layout = QFormLayout(self.transform_group)
        self.x_spin = self._make_transform_spin()
        self.x_spin.setToolTip(
            "Exact X position (µm) of the instance origin — a precise, typed "
            "alternative to dragging on the canvas. Takes effect on Apply Transform."
        )
        self.y_spin = self._make_transform_spin()
        self.y_spin.setToolTip(
            "Exact Y position (µm) of the instance origin — a precise, typed "
            "alternative to dragging on the canvas. Takes effect on Apply Transform."
        )
        self.rotation_spin = self._make_transform_spin(minimum=-360.0, maximum=360.0, decimals=2)
        self.rotation_spin.setToolTip(
            "Rotation in degrees (counter-clockwise). Normalized to 0–360 when applied."
        )
        self.mirror_check = QCheckBox()
        self.mirror_check.setToolTip(
            "Reflect the instance about its x-axis (flip top-to-bottom). "
            "Applied before scale and rotation."
        )
        self.scale_spin = self._make_transform_spin(minimum=0.001, maximum=1000.0, decimals=4)
        self.scale_spin.setToolTip(
            "Uniform magnification factor (1 = original size)."
        )
        transform_layout.addRow("X (µm)", self.x_spin)
        transform_layout.addRow("Y (µm)", self.y_spin)
        transform_layout.addRow("Rotation (°)", self.rotation_spin)
        transform_layout.addRow("Mirror", self.mirror_check)
        transform_layout.addRow("Scale", self.scale_spin)
        self.apply_transform_button = QPushButton("Apply Transform")
        self.apply_transform_button.setToolTip(
            "Apply the X/Y, rotation, mirror and scale above to the selected "
            "instance as a single undoable move."
        )
        self.apply_transform_button.clicked.connect(self._on_apply_transform)
        transform_layout.addRow(self.apply_transform_button)
        layout.addWidget(self.transform_group)

        self.array_group = QGroupBox("Array")
        array_layout = QFormLayout(self.array_group)
        self.columns_spin = self._make_count_spin()
        self.columns_spin.setToolTip(
            "Number of columns when tiling this instance into a rectangular array (1 = no array)."
        )
        self.rows_spin = self._make_count_spin()
        self.rows_spin.setToolTip(
            "Number of rows when tiling this instance into a rectangular array (1 = no array)."
        )
        self.column_pitch_spin = self._make_transform_spin(minimum=-1e6, maximum=1e6, decimals=4)
        self.column_pitch_spin.setToolTip(
            "Spacing (µm) between array columns. Auto-seeded from the instance's own "
            "width the first time you raise the column count above 1 so copies don't stack."
        )
        self.row_pitch_spin = self._make_transform_spin(minimum=-1e6, maximum=1e6, decimals=4)
        self.row_pitch_spin.setToolTip(
            "Spacing (µm) between array rows. Auto-seeded from the instance's own "
            "height the first time you raise the row count above 1 so copies don't stack."
        )
        # Bumping a count above 1 with the pitch still at 0 would stack every
        # copy on top of the original — seed the pitch from the component's
        # own size so a fresh array is immediately visible/sensible.
        self.columns_spin.valueChanged.connect(lambda v: self._seed_pitch(self.column_pitch_spin, v, 0))
        self.rows_spin.valueChanged.connect(lambda v: self._seed_pitch(self.row_pitch_spin, v, 1))
        array_layout.addRow("Columns", self.columns_spin)
        array_layout.addRow("Rows", self.rows_spin)
        array_layout.addRow("Column pitch (µm)", self.column_pitch_spin)
        array_layout.addRow("Row pitch (µm)", self.row_pitch_spin)
        self.apply_array_button = QPushButton("Apply Array")
        self.apply_array_button.setToolTip(
            "Tile the selected instance into a columns×rows array using the pitches above (undoable)."
        )
        self.apply_array_button.clicked.connect(self._on_apply_array)
        array_layout.addRow(self.apply_array_button)
        layout.addWidget(self.array_group)

        # Route group — shown only when a placed route is selected (see
        # show_route). Lets a route's length goal be edited after placement:
        # re-runs its adiabatic meander to hit a new target, the same machinery
        # the routing toolbar uses at creation time.
        self.route_group = QGroupBox("Route")
        route_layout = QFormLayout(self.route_group)
        self.route_length_label = QLabel("—")
        self.route_length_label.setToolTip("The route's current physical length (and the delay it implies).")
        route_layout.addRow("Length", self.route_length_label)

        target_row = QWidget()
        target_hbox = QHBoxLayout(target_row)
        target_hbox.setContentsMargins(0, 0, 0, 0)
        self.route_target_spin = QDoubleSpinBox()
        self.route_target_spin.setRange(0.0, 1e9)
        self.route_target_spin.setDecimals(3)
        self.route_target_spin.setToolTip(
            "Target length for this route. Phidler inserts an adiabatic meander to "
            "approach it (bounded by the room and bend radius available). 0 removes "
            "the goal and routes it directly."
        )
        self.route_target_unit = QComboBox()
        self.route_target_unit.addItems(["µm", "fs", "ns"])
        self.route_target_unit.setToolTip(
            "Interpret the target as a physical length (µm) or a propagation delay "
            "(fs/ns), converted with the current effective index."
        )
        target_hbox.addWidget(self.route_target_spin, stretch=1)
        target_hbox.addWidget(self.route_target_unit)
        route_layout.addRow("Target", target_row)

        self.route_auto_check = QCheckBox("Meander to match")
        self.route_auto_check.setChecked(True)
        self.route_auto_check.setToolTip(
            "Insert an adiabatic meander to reach the target length. Off routes "
            "directly (the target is then just recorded, not enforced)."
        )
        route_layout.addRow(self.route_auto_check)

        self.apply_route_length_button = QPushButton("Apply Length")
        self.apply_route_length_button.setToolTip(
            "Re-route with the target above as one undoable step."
        )
        self.apply_route_length_button.clicked.connect(self._on_apply_route_length)
        route_layout.addRow(self.apply_route_length_button)
        self.route_group.setVisible(False)
        layout.addWidget(self.route_group)

        self.form_layout = QFormLayout()
        layout.addLayout(self.form_layout)

        self.apply_button = QPushButton("Apply")
        self.apply_button.setToolTip(
            "Rebuild the selected instance with the parameter values above. If the "
            "rebuild fails the change is reverted and the error shown in the status bar."
        )
        self.apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self.apply_button)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.setEnabled(False)

    @staticmethod
    def _make_transform_spin(minimum: float = -1e6, maximum: float = 1e6, decimals: int = 4) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        return spin

    @staticmethod
    def _make_count_spin() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1, 10_000)
        return spin

    def _seed_pitch(self, pitch_spin: QDoubleSpinBox, count: int, axis: int) -> None:
        if count > 1 and pitch_spin.value() == 0.0 and self._bbox_extent[axis] > 0.0:
            pitch_spin.setValue(self._bbox_extent[axis])

    def show_instance(
        self,
        inst_id: int,
        component_spec: str,
        signature: inspect.Signature,
        current_kwargs: dict[str, Any],
        columns: int = 1,
        rows: int = 1,
        column_pitch: float = 0.0,
        row_pitch: float = 0.0,
        bbox_extent: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self._inst_id = inst_id
        self._route_id = None
        self._fields = {}
        self._show_instance_groups(True)
        self.title_label.setText(f"#{inst_id}: {component_spec}")
        self._clear_form()
        self._set_array_fields(columns, rows, column_pitch, row_pitch, bbox_extent)

        for p in signature.parameters.values():
            if p.default is inspect.Parameter.empty:
                continue
            value = current_kwargs.get(p.name, p.default)
            widget = self._make_field(p.name, value)
            unit = _unit_for_param(p.name, value)
            label = f"{p.name} ({unit})" if unit else p.name
            self.form_layout.addRow(label, widget)
            self._fields[p.name] = widget

        self.setEnabled(True)

    def update_transform(self, x: float, y: float, rotation: float, mirror: bool, mag: float) -> None:
        """Syncs the transform fields to the instance's current values —
        called on selection change and from the same periodic timer that
        keeps the on-canvas transform handles positioned, so a drag/handle
        gesture is reflected here too. Skips the sync entirely while the
        user has focus in one of these fields, the same is-interacting
        guard the handles use for their own periodic resync, so typing
        isn't clobbered mid-edit."""
        if self._is_editing_transform():
            return
        self.x_spin.blockSignals(True)
        self.y_spin.blockSignals(True)
        self.rotation_spin.blockSignals(True)
        self.mirror_check.blockSignals(True)
        self.scale_spin.blockSignals(True)
        self.x_spin.setValue(x)
        self.y_spin.setValue(y)
        self.rotation_spin.setValue(rotation)
        self.mirror_check.setChecked(mirror)
        self.scale_spin.setValue(mag)
        self.x_spin.blockSignals(False)
        self.y_spin.blockSignals(False)
        self.rotation_spin.blockSignals(False)
        self.mirror_check.blockSignals(False)
        self.scale_spin.blockSignals(False)

    def _is_editing_transform(self) -> bool:
        return any(
            w.hasFocus() for w in (self.x_spin, self.y_spin, self.rotation_spin, self.mirror_check, self.scale_spin)
        )

    def _on_apply_transform(self) -> None:
        if self._inst_id is None:
            return
        self.transform_applied.emit(
            self._inst_id,
            self.x_spin.value(),
            self.y_spin.value(),
            self.rotation_spin.value(),
            self.mirror_check.isChecked(),
            self.scale_spin.value(),
        )

    def _set_array_fields(
        self, columns: int, rows: int, column_pitch: float, row_pitch: float, bbox_extent: tuple[float, float]
    ) -> None:
        self._bbox_extent = bbox_extent
        for spin, value in (
            (self.columns_spin, columns),
            (self.rows_spin, rows),
            (self.column_pitch_spin, column_pitch),
            (self.row_pitch_spin, row_pitch),
        ):
            spin.blockSignals(True)  # don't let _seed_pitch fire while seeding values
            spin.setValue(value)
            spin.blockSignals(False)

    def _on_apply_array(self) -> None:
        if self._inst_id is None:
            return
        self.array_applied.emit(
            self._inst_id,
            self.columns_spin.value(),
            self.rows_spin.value(),
            self.column_pitch_spin.value(),
            self.row_pitch_spin.value(),
        )

    def show_route(
        self,
        route_id: int,
        length_um: float,
        goal_um: float | None,
        auto: bool,
        time_str: str = "",
    ) -> None:
        """Show the route-length editor for a selected placed route. ``goal_um``
        is the current target (None if the route has none) and ``time_str`` is a
        preformatted propagation-delay string shown next to the length."""
        self._route_id = route_id
        self._inst_id = None
        self._fields = {}
        self._clear_form()
        self._show_instance_groups(False)
        self.title_label.setText(f"Route #{route_id}")
        detail = f"{length_um:.3f} µm" + (f"   ·   {time_str}" if time_str else "")
        self.route_length_label.setText(detail)
        self.route_target_spin.blockSignals(True)
        self.route_target_unit.setCurrentText("µm")  # current goal is stored in µm
        self.route_target_spin.setValue(goal_um if goal_um else 0.0)
        self.route_target_spin.blockSignals(False)
        self.route_auto_check.setChecked(auto if goal_um else True)
        self.route_group.setVisible(True)
        self.setEnabled(True)

    def _show_instance_groups(self, visible: bool) -> None:
        """Toggle the instance-only sections (transform/array/params/apply) as a
        unit, and hide the route group when they're shown. Keeps the panel in
        exactly one of two modes: instance editor or route editor."""
        self.transform_group.setVisible(visible)
        self.array_group.setVisible(visible)
        self.apply_button.setVisible(visible)
        if visible:
            self.route_group.setVisible(False)

    def _on_apply_route_length(self) -> None:
        if self._route_id is None:
            return
        self.route_length_applied.emit(
            self._route_id,
            self.route_target_spin.value(),
            self.route_target_unit.currentText(),
            self.route_auto_check.isChecked(),
        )

    def clear(self) -> None:
        self._inst_id = None
        self._route_id = None
        self._fields = {}
        self._show_instance_groups(True)
        self.title_label.setText("No selection")
        self._clear_form()
        self.setEnabled(False)

    def _clear_form(self) -> None:
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)

    @staticmethod
    def _make_field(name: str, value: Any) -> QWidget:
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
            return w
        if isinstance(value, int):
            w = QSpinBox()
            w.setRange(-1_000_000, 1_000_000)
            w.setValue(value)
            return w
        if isinstance(value, float):
            w = QDoubleSpinBox()
            w.setRange(-1e6, 1e6)
            w.setDecimals(4)
            w.setValue(value)
            return w
        if isinstance(value, str):
            list_options = _VOCABULARY_FIELDS.get(name)
            if list_options is not None:
                w = QComboBox()
                options = list_options()
                w.addItems(options)
                if value in options:
                    w.setCurrentText(value)
                return w
            return QLineEdit(value)
        w = QLineEdit(repr(value))
        w.setEnabled(False)
        w.setToolTip("This parameter type isn't editable from the panel yet.")
        return w

    def _on_apply(self) -> None:
        if self._inst_id is None:
            return
        kwargs: dict[str, Any] = {}
        for name, w in self._fields.items():
            if isinstance(w, QCheckBox):
                kwargs[name] = w.isChecked()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                kwargs[name] = w.value()
            elif isinstance(w, QComboBox):
                kwargs[name] = w.currentText()
            elif isinstance(w, QLineEdit) and w.isEnabled():
                kwargs[name] = w.text()
        self.params_applied.emit(self._inst_id, kwargs)
