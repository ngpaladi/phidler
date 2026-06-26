from __future__ import annotations

import gdsfactory as gf
from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence, QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
    QToolBar,
)

from phidler.canvas.scene import LayoutScene
from phidler.canvas.transform_handles import TransformHandleSet
from phidler.canvas.view import UNIT_MODES, LayoutView
from phidler.custom_components import load_custom_components
from phidler.drc import run_drc
from phidler.export_script import export_python_script
from phidler.import_script import load_python_script
from phidler.model.commands import (
    AddInstanceCommand,
    AddRouteCommand,
    DeleteInstanceCommand,
    DeleteRouteCommand,
    EditParamsCommand,
    MoveInstanceCommand,
)
from phidler.model.document import LayoutDocument, Transform
from phidler.panels.component_palette import ComponentPalette
from phidler.panels.console_panel import ConsolePanel
from phidler.panels.drc_panel import DrcPanel
from phidler.panels.layers_panel import LayersPanel
from phidler.panels.project_settings_dialog import ProjectSettingsDialog
from phidler.panels.properties_panel import PropertiesPanel
from phidler.pdk_catalog import build_catalog, list_cross_section_names
from phidler.project_io import load_project, save_project


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Phidler — PIC Layout CAD")
        self.resize(1200, 800)

        self.document = LayoutDocument()
        self.scene = LayoutScene(self.document, parent=self)
        self.undo_stack = QUndoStack(self)
        self.view = LayoutView(self.scene, undo_stack=self.undo_stack)
        self.view.set_n_eff(self.document.project_settings.core_index)
        self.setCentralWidget(self.view)

        self._clipboard: list[tuple[str, dict]] = []
        self.catalog = build_catalog()
        self.catalog_by_name = {spec.name: spec for specs in self.catalog.values() for spec in specs}
        self._pending_route_port: tuple[int, str] | None = None
        self.route_cross_section = "strip"
        self.project_path: str | None = None

        self.setStatusBar(QStatusBar())
        self.cursor_pos_label = QLabel("")
        self.statusBar().addPermanentWidget(self.cursor_pos_label)
        self.view.instances_moved.connect(self._on_instances_moved)
        self.view.placement_requested.connect(self._on_placement_requested)
        self.view.routing_mode_changed.connect(self._on_routing_mode_changed)
        self.view.measure_mode_changed.connect(self._on_measure_mode_changed)
        self.view.measurement_taken.connect(self._on_measurement_taken)
        self.view.cursor_position_changed.connect(self._on_cursor_position_changed)
        self.scene.selectionChanged.connect(self._on_selection_changed)
        self.scene.port_clicked.connect(self._on_port_clicked)

        self._build_palette_panel()
        self._build_properties_panel()
        self._build_layers_panel()
        self._build_drc_panel()
        self._build_fdtd_panel()
        self._build_console_panel()
        self._build_transform_overlay()
        self._build_toolbar()
        self._build_menus()
        self.undo_stack.indexChanged.connect(self._on_undo_index_changed)

    # -- panels -------------------------------------------------------------

    def _build_palette_panel(self) -> None:
        self.palette = ComponentPalette(self.catalog)
        self.palette.place_requested.connect(self.view.arm_placement)

        dock = QDockWidget("Components", self)
        dock.setWidget(self.palette)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _build_properties_panel(self) -> None:
        self.properties_panel = PropertiesPanel()
        self.properties_panel.params_applied.connect(self._on_params_applied)
        self.properties_panel.transform_applied.connect(self._on_properties_transform_applied)

        dock = QDockWidget("Properties", self)
        dock.setWidget(self.properties_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    def _build_layers_panel(self) -> None:
        self.layers_panel = LayersPanel()
        self.layers_panel.refresh(self.document.layers)
        self.layers_panel.visibility_changed.connect(self._on_layer_visibility_changed)
        self.layers_panel.color_changed.connect(self._on_layer_color_changed)

        dock = QDockWidget("Layers", self)
        dock.setWidget(self.layers_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    def _on_layer_visibility_changed(self, key: tuple, visible: bool) -> None:
        if key in self.document.layers:
            self.document.layers[key].visible = visible
        self.scene.set_layer_visible(key, visible)

    def _on_layer_color_changed(self, key: tuple, color: str) -> None:
        if key in self.document.layers:
            self.document.layers[key].color = color
        self.scene.set_layer_color(key, color)

    def _build_drc_panel(self) -> None:
        self.drc_panel = DrcPanel()
        self.drc_panel.set_layers(self.document.layers)
        self.drc_panel.run_requested.connect(self._on_run_drc)
        self.drc_panel.violation_selected.connect(self._on_violation_selected)

        dock = QDockWidget("DRC", self)
        dock.setWidget(self.drc_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    def _build_fdtd_panel(self) -> None:
        """Despite the name (kept so the existing early call site in
        __init__ doesn't need to move), this no longer builds a dock — FDTD
        simulation is a separate top-level window now, opened on demand via
        the Simulate menu, not always present like the other docks."""
        self._fdtd_window = None
        simulate_menu = self.menuBar().addMenu("&Simulate")
        self.fdtd_window_action = simulate_menu.addAction("FDTD Simulation…")
        self.fdtd_window_action.triggered.connect(self._open_fdtd_window)

    def _open_fdtd_window(self) -> None:
        if self._fdtd_window is not None:
            self._fdtd_window.show()
            self._fdtd_window.raise_()
            self._fdtd_window.activateWindow()
            return

        try:
            from phidler.panels.fdtd_window import FdtdWindow
        except ImportError as exc:
            # photonfdtd/matplotlib are the optional `fdtd` extras, not a
            # core dependency — a user without them installed still gets
            # a working app, just with this explanatory message instead
            # of a crash at startup.
            QMessageBox.warning(
                self,
                "FDTD Simulation unavailable",
                "FDTD simulation requires the optional 'fdtd' extras "
                f"(photonfdtd + matplotlib), which aren't installed:\n{exc}\n\n"
                'Install with: pip install -e ".[fdtd]"\n'
                "(photonfdtd isn't on PyPI yet — see pyproject.toml)",
            )
            return

        self._fdtd_window = FdtdWindow(self.document, self.view, parent=self)
        self._fdtd_window.show()

    def _build_console_panel(self) -> None:
        def place(
            component_spec: str,
            x: float = 0.0,
            y: float = 0.0,
            rotation: float = 0.0,
            mirror: bool = False,
            **kwargs,
        ):
            """Places a component immediately, rendered right away. Not
            pushed onto the undo stack — use the palette/canvas for that."""
            inst = self.document.add_instance(component_spec, kwargs, x=x, y=y, rotation=rotation, mirror=mirror)
            self.scene.add_instance_item(inst.id)
            return inst

        def route(inst_a_id: int, port_a: str, inst_b_id: int, port_b: str, cross_section: str = "strip"):
            """Routes between two ports immediately, rendered right away.
            Not pushed onto the undo stack."""
            placed = self.document.add_route(inst_a_id, port_a, inst_b_id, port_b, cross_section)
            self.scene.add_route_item(placed.id)
            return placed

        namespace = {
            "gf": gf,
            "doc": self.document,
            "scene": self.scene,
            "view": self.view,
            "win": self,
            "place": place,
            "route": route,
        }
        self.console_panel = ConsolePanel(namespace)

        dock = QDockWidget("Console", self)
        dock.setWidget(self.console_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.console_toggle_action = dock.toggleViewAction()

    def _build_transform_overlay(self) -> None:
        self.transform_handles = TransformHandleSet(self.scene, self.document, self.undo_stack)

        # A periodic refresh, not signal-driven hooks into every possible
        # view-mutating interaction (pan/zoom/resize/drag-in-progress):
        # simpler and more robust than trying to enumerate every path that
        # could move the selected item on screen. Skipped while a handle
        # is mid-drag, or the live drag position would be clobbered by a
        # stale re-read of the document's not-yet-committed transform.
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setInterval(120)
        self._overlay_timer.timeout.connect(self._update_transform_overlay)
        self._overlay_timer.start()

    def _selected_single_instance_id(self) -> int | None:
        ids = self._selected_instance_ids()
        return ids[0] if len(ids) == 1 else None

    def _update_transform_overlay(self) -> None:
        inst_id = self._selected_single_instance_id()
        if inst_id is None or inst_id not in self.scene.items_by_inst:
            self.transform_handles.hide()
            return
        # The properties panel reads the document's committed transform,
        # not the handle's live in-progress drag position — so during an
        # active handle drag this still shows the pre-drag value until the
        # drag commits on release, rather than tracking the live preview.
        # A reasonable v1 simplification: update_transform() already
        # has its own guard against clobbering the user's own typing
        # (_is_editing_transform), so this is safe to call unconditionally.
        t = self.document.get_transform(inst_id)
        self.properties_panel.update_transform(t.x, t.y, t.rotation, t.mirror, t.mag)

        if self.transform_handles.is_interacting():
            return
        self.transform_handles.show_for(inst_id)

    def _on_undo_index_changed(self, _index: int) -> None:
        self.layers_panel.refresh(self.document.layers)
        self.drc_panel.set_layers(self.document.layers)

    def _on_run_drc(self, layer_key: tuple, min_width: float, min_spacing: float) -> None:
        violations = run_drc(self.document, layer_key, min_width, min_spacing)
        self.drc_panel.show_results(violations)
        self.scene.show_drc_violations(violations)
        self.statusBar().showMessage(f"DRC: {len(violations)} violation(s) against entered thresholds", 5000)

    def _on_violation_selected(self, left: float, bottom: float, right: float, top: float) -> None:
        self.view.centerOn(QPointF((left + right) / 2, (bottom + top) / 2))

    # -- toolbar / menus ------------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)

        place_action = toolbar.addAction("Place Straight Waveguide")
        place_action.triggered.connect(self._place_straight_waveguide)

        self.route_action = toolbar.addAction("Route")
        self.route_action.setCheckable(True)
        self.route_action.setToolTip("Click a port, then click another port to route between them (Esc to exit)")
        self.route_action.toggled.connect(self.view.set_routing_mode)

        self.measure_action = toolbar.addAction("Measure")
        self.measure_action.setCheckable(True)
        self.measure_action.setToolTip(
            "Click two points (snaps to a nearby port) to show the distance between them (Esc to exit)"
        )
        self.measure_action.toggled.connect(self.view.set_measure_mode)

        toolbar.addWidget(QLabel(" Cross-section: "))
        self.cross_section_combo = QComboBox()
        self.cross_section_combo.addItems(list_cross_section_names())
        self.cross_section_combo.setCurrentText(self.route_cross_section)
        self.cross_section_combo.currentTextChanged.connect(self._on_route_cross_section_changed)
        toolbar.addWidget(self.cross_section_combo)

        toolbar.addWidget(QLabel(" Grid (µm): "))
        self.grid_pitch_spin = QDoubleSpinBox()
        self.grid_pitch_spin.setDecimals(3)
        self.grid_pitch_spin.setRange(0.001, 1000.0)  # > 0: drawBackground's pitch-scaling loop requires it
        self.grid_pitch_spin.setSingleStep(0.1)
        self.grid_pitch_spin.setValue(self.view.grid_pitch)
        self.grid_pitch_spin.valueChanged.connect(self._on_grid_pitch_changed)
        toolbar.addWidget(self.grid_pitch_spin)

        self.snap_checkbox = QCheckBox("Snap")
        self.snap_checkbox.setChecked(self.view.snap_enabled)
        self.snap_checkbox.toggled.connect(self._on_snap_enabled_changed)
        toolbar.addWidget(self.snap_checkbox)

        toolbar.addWidget(QLabel(" Units: "))
        self.units_combo = QComboBox()
        for label, _ in UNIT_MODES:
            self.units_combo.addItem(label)
        self.units_combo.setToolTip(
            "Switch coordinate display between spatial µm and propagation time.\n"
            "Propagation time uses the effective phase index from the last\n"
            "mode solve (or the core index if no solve has been run)."
        )
        self.units_combo.currentIndexChanged.connect(self._on_unit_mode_changed)
        toolbar.addWidget(self.units_combo)

        export_action = toolbar.addAction("Export GDS…")
        export_action.triggered.connect(self._export_gds)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        new_action = file_menu.addAction("New")
        new_action.setShortcut(QKeySequence.New)
        new_action.triggered.connect(self._new_project)

        project_settings_action = file_menu.addAction("Project Settings…")
        project_settings_action.triggered.connect(self._edit_project_settings)

        file_menu.addSeparator()

        open_action = file_menu.addAction("Open…")
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self._open_project)

        save_action = file_menu.addAction("Save")
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self._save_project)

        save_as_action = file_menu.addAction("Save As…")
        save_as_action.setShortcut(QKeySequence.SaveAs)
        save_as_action.triggered.connect(self._save_project_as)

        file_menu.addSeparator()

        import_ref_action = file_menu.addAction("Import Reference GDS…")
        import_ref_action.triggered.connect(self._import_reference_gds)

        clear_ref_action = file_menu.addAction("Clear Reference")
        clear_ref_action.triggered.connect(self._clear_reference_gds)

        file_menu.addSeparator()

        import_custom_action = file_menu.addAction("Import Custom Components…")
        import_custom_action.triggered.connect(self._import_custom_components)

        file_menu.addSeparator()

        export_action = file_menu.addAction("Export GDS…")
        export_action.triggered.connect(self._export_gds)

        export_script_action = file_menu.addAction("Export Python Script…")
        export_script_action.triggered.connect(self._export_python_script)

        edit_menu = self.menuBar().addMenu("&Edit")

        undo_action = self.undo_stack.createUndoAction(self, "Undo")
        undo_action.setShortcut(QKeySequence.Undo)
        edit_menu.addAction(undo_action)

        redo_action = self.undo_stack.createRedoAction(self, "Redo")
        redo_action.setShortcut(QKeySequence.Redo)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        # Stored as self.<x>_action (not local vars) so the canvas
        # right-click context menu can reuse these exact QAction objects
        # instead of duplicating their wiring.
        self.delete_action = edit_menu.addAction("Delete")
        self.delete_action.setShortcut(QKeySequence.Delete)
        self.delete_action.triggered.connect(self._delete_selected)

        self.rotate_action = edit_menu.addAction("Rotate 90°")
        self.rotate_action.setShortcut("R")
        self.rotate_action.triggered.connect(self._rotate_selected)

        self.mirror_action = edit_menu.addAction("Mirror")
        self.mirror_action.setShortcut("M")
        self.mirror_action.triggered.connect(self._mirror_selected)

        self.reset_transform_action = edit_menu.addAction("Reset Transform")
        self.reset_transform_action.triggered.connect(self._reset_selected_transform)

        self._build_align_actions()
        align_menu = edit_menu.addMenu("Align")
        for action in self.align_actions:
            align_menu.addAction(action)
        align_menu.addSeparator()
        for action in self.distribute_actions:
            align_menu.addAction(action)

        edit_menu.addSeparator()

        self.copy_action = edit_menu.addAction("Copy")
        self.copy_action.setShortcut(QKeySequence.Copy)
        self.copy_action.triggered.connect(self._copy_selected)

        self.paste_action = edit_menu.addAction("Paste")
        self.paste_action.setShortcut(QKeySequence.Paste)
        self.paste_action.triggered.connect(self._paste_clipboard)

        edit_menu.addSeparator()

        self.select_all_action = edit_menu.addAction("Select All")
        self.select_all_action.setShortcut(QKeySequence.SelectAll)
        self.select_all_action.triggered.connect(self._select_all)

        view_menu = self.menuBar().addMenu("&View")

        self.zoom_fit_action = view_menu.addAction("Zoom to Fit")
        self.zoom_fit_action.setShortcut("Ctrl+0")
        self.zoom_fit_action.triggered.connect(self.view.zoom_to_fit)

        self.zoom_selection_action = view_menu.addAction("Zoom to Selection")
        self.zoom_selection_action.setShortcut("Ctrl+Shift+0")
        self.zoom_selection_action.triggered.connect(self.view.zoom_to_selection)

        view_menu.addSeparator()
        self.console_toggle_action.setText("Console")
        view_menu.addAction(self.console_toggle_action)

        view_menu.addSeparator()
        self.fullscreen_action = view_menu.addAction("Full Screen")
        self.fullscreen_action.setShortcut("F11")
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.triggered.connect(self._toggle_fullscreen)

        self.view.context_menu_requested.connect(self._show_canvas_context_menu)

    def _on_cursor_position_changed(self, x: float, y: float) -> None:
        x_d = self.view.um_to_display(x)
        y_d = self.view.um_to_display(y)
        unit = self.view.unit_str()
        self.cursor_pos_label.setText(f"X: {x_d:.3f} {unit}   Y: {y_d:.3f} {unit}")

    def _on_unit_mode_changed(self, idx: int) -> None:
        mode = UNIT_MODES[idx][1]
        self.view.set_unit_mode(mode)
        # Propagate to the FDTD window's field views if it has been opened
        if self._fdtd_window is not None:
            self._fdtd_window.mode_view.set_unit_mode(mode)
            self._fdtd_window.run_view.set_unit_mode(mode)

    # -- actions --------------------------------------------------------------

    def _place_straight_waveguide(self) -> None:
        command = AddInstanceCommand(self.document, self.scene, "straight", {"length": 10.0, "width": 0.5})
        self._push_add_instance(command, "straight")

    def _on_placement_requested(self, component_spec: str, x: float, y: float) -> None:
        command = AddInstanceCommand(self.document, self.scene, component_spec, {}, x=x, y=y)
        self._push_add_instance(command, component_spec)

    def _push_add_instance(self, command: AddInstanceCommand, component_spec: str) -> None:
        self.undo_stack.push(command)
        if command.error is not None:
            self.undo_stack.undo()  # pop the no-op command back off the stack
            self.statusBar().showMessage(f"Could not place {component_spec}: {command.error}", 6000)
            return
        self.statusBar().showMessage(f"Placed {component_spec} (instance #{command.inst_id})", 3000)

    def _selected_instance_ids(self) -> list[int]:
        return [item.inst_id for item in self.scene.selectedItems() if not item.is_route]

    def _selected_route_ids(self) -> list[int]:
        return [item.inst_id for item in self.scene.selectedItems() if item.is_route]

    def _on_routing_mode_changed(self, enabled: bool) -> None:
        self.route_action.setChecked(enabled)
        if not enabled:
            self._pending_route_port = None
        else:
            self.statusBar().showMessage("Route: click a port to start", 3000)

    def _on_measure_mode_changed(self, enabled: bool) -> None:
        self.measure_action.setChecked(enabled)
        if enabled:
            self.statusBar().showMessage("Measure: click a first point", 3000)

    def _on_measurement_taken(self, dx: float, dy: float, distance: float) -> None:
        self.statusBar().showMessage(f"Distance: {distance:.3f} µm  (dx={dx:.3f}, dy={dy:.3f})", 8000)

    def _on_route_cross_section_changed(self, name: str) -> None:
        self.route_cross_section = name

    def _on_grid_pitch_changed(self, value: float) -> None:
        self.view.grid_pitch = value
        self.view.viewport().update()

    def _on_snap_enabled_changed(self, enabled: bool) -> None:
        self.view.snap_enabled = enabled

    def _on_port_clicked(self, inst_id: int, port_name: str) -> None:
        if self._pending_route_port is None:
            self._pending_route_port = (inst_id, port_name)
            self.statusBar().showMessage(f"Route: click the second port (from #{inst_id}:{port_name})", 5000)
            return
        a_inst_id, a_port = self._pending_route_port
        self._pending_route_port = None
        if (a_inst_id, a_port) == (inst_id, port_name):
            return
        command = AddRouteCommand(
            self.document, self.scene, a_inst_id, a_port, inst_id, port_name, cross_section=self.route_cross_section
        )
        self.undo_stack.push(command)
        if command.error is not None:
            self.undo_stack.undo()  # pop the no-op command back off the stack
            self.statusBar().showMessage(f"Routing failed: {command.error}", 5000)
            return
        self.statusBar().showMessage(f"Routed #{a_inst_id}:{a_port} -> #{inst_id}:{port_name}", 3000)

    def _on_selection_changed(self) -> None:
        ids = self._selected_instance_ids()
        if len(ids) != 1:
            self.properties_panel.clear()
            return
        inst_id = ids[0]
        inst = self.document.instances[inst_id]
        spec = self.catalog_by_name.get(inst.component_spec)
        if spec is None:
            self.properties_panel.clear()
            return
        self.properties_panel.show_instance(inst_id, inst.component_spec, spec.signature, inst.kwargs)
        t = self.document.get_transform(inst_id)
        self.properties_panel.update_transform(t.x, t.y, t.rotation, t.mirror, t.mag)

    def _on_properties_transform_applied(self, inst_id: int, x: float, y: float, rotation: float, mirror: bool, mag: float) -> None:
        if inst_id not in self.document.instances:
            return
        old_t = self.document.get_transform(inst_id)
        new_t = Transform(x=x, y=y, rotation=rotation % 360.0, mirror=mirror, mag=max(mag, 1e-6))
        self.undo_stack.push(MoveInstanceCommand(self.document, self.scene, inst_id, old_t, new_t))

    def _on_params_applied(self, inst_id: int, new_kwargs: dict) -> None:
        old_kwargs = dict(self.document.instances[inst_id].kwargs)
        command = EditParamsCommand(self.document, self.scene, inst_id, old_kwargs, new_kwargs)
        self.undo_stack.push(command)
        if command.error is not None:
            self.undo_stack.undo()  # pop the no-op command back off the stack
            self.statusBar().showMessage(f"Parameter update failed: {command.error}", 5000)
            return
        self.statusBar().showMessage(f"Updated parameters for instance #{inst_id}", 2000)

    def _delete_selected(self) -> None:
        inst_ids = self._selected_instance_ids()
        route_ids = self._selected_route_ids()
        if not inst_ids and not route_ids:
            return
        # Routes must be pushed before instances: QUndoStack undoes a
        # macro's children in reverse push order, so this way undo restores
        # instances first and routes second — by the time a route's undo()
        # calls add_route() and looks up its endpoint instances, they're
        # already back. Pushing the other way round made a route's undo()
        # raise KeyError on a not-yet-restored endpoint, aborting the macro
        # partway through (confirmed empirically before this fix).
        self.undo_stack.beginMacro("Delete")
        for route_id in route_ids:
            self.undo_stack.push(DeleteRouteCommand(self.document, self.scene, route_id))
        for inst_id in inst_ids:
            self.undo_stack.push(DeleteInstanceCommand(self.document, self.scene, inst_id))
        self.undo_stack.endMacro()
        self.statusBar().showMessage(f"Deleted {len(inst_ids)} instance(s), {len(route_ids)} route(s)", 2000)

    def _rotate_selected(self) -> None:
        ids = self._selected_instance_ids()
        if not ids:
            return
        self.undo_stack.beginMacro("Rotate")
        for inst_id in ids:
            old_t = self.document.get_transform(inst_id)
            # mag must be carried over explicitly — Transform's mag field
            # defaults to 1.0, so omitting it here would silently reset
            # any applied scale back to 100% on every rotate.
            new_t = Transform(
                x=old_t.x, y=old_t.y, rotation=(old_t.rotation + 90.0) % 360.0, mirror=old_t.mirror, mag=old_t.mag
            )
            self.undo_stack.push(MoveInstanceCommand(self.document, self.scene, inst_id, old_t, new_t))
        self.undo_stack.endMacro()

    def _mirror_selected(self) -> None:
        ids = self._selected_instance_ids()
        if not ids:
            return
        self.undo_stack.beginMacro("Mirror")
        for inst_id in ids:
            old_t = self.document.get_transform(inst_id)
            new_t = Transform(x=old_t.x, y=old_t.y, rotation=old_t.rotation, mirror=not old_t.mirror, mag=old_t.mag)
            self.undo_stack.push(MoveInstanceCommand(self.document, self.scene, inst_id, old_t, new_t))
        self.undo_stack.endMacro()

    def _reset_selected_transform(self) -> None:
        """Clears rotation/mirror/scale back to defaults — position is left
        untouched. Replaces the old transform-overlay panel's Reset button
        now that rotate/scale are on-canvas drag gestures rather than a
        widget with its own button to put this on."""
        ids = self._selected_instance_ids()
        if not ids:
            return
        self.undo_stack.beginMacro("Reset Transform")
        for inst_id in ids:
            old_t = self.document.get_transform(inst_id)
            new_t = Transform(x=old_t.x, y=old_t.y, rotation=0.0, mirror=False, mag=1.0)
            self.undo_stack.push(MoveInstanceCommand(self.document, self.scene, inst_id, old_t, new_t))
        self.undo_stack.endMacro()

    def _selected_scene_bboxes(self) -> dict[int, QRectF]:
        """Each selected instance's axis-aligned bounding box in absolute
        scene coordinates — normalized explicitly rather than trusting
        QGraphicsItem.mapRectToScene()'s result to already have min<max in
        both axes, after finding empirically that a plain QRectF's
        top()/bottom()/left()/right() just return whatever min/max order
        the rect happened to be constructed with, not a guaranteed
        normalized min/max, unless you call .normalized() yourself."""
        ids = self._selected_instance_ids()
        boxes = {}
        for inst_id in ids:
            item = self.scene.items_by_inst.get(inst_id)
            if item is not None:
                boxes[inst_id] = item.mapRectToScene(item.boundingRect()).normalized()
        return boxes

    def _apply_axis_shifts(self, shifts: dict[int, float], axis: str, macro_name: str) -> None:
        """shifts: {inst_id: delta} to add to that instance's x (axis='x')
        or y (axis='y') — the other axis, rotation, mirror, and mag are
        left untouched. Skips instances whose shift is ~0 so a no-op
        align (already-aligned instance) doesn't push a useless undo
        entry."""
        nonzero = {i: d for i, d in shifts.items() if abs(d) > 1e-9}
        if not nonzero:
            return
        self.undo_stack.beginMacro(macro_name)
        for inst_id, delta in nonzero.items():
            old_t = self.document.get_transform(inst_id)
            new_x = old_t.x + delta if axis == "x" else old_t.x
            new_y = old_t.y + delta if axis == "y" else old_t.y
            new_t = Transform(x=new_x, y=new_y, rotation=old_t.rotation, mirror=old_t.mirror, mag=old_t.mag)
            self.undo_stack.push(MoveInstanceCommand(self.document, self.scene, inst_id, old_t, new_t))
        self.undo_stack.endMacro()

    def _build_align_actions(self) -> None:
        """Stored as self.align_actions/self.distribute_actions (not local
        vars) so the canvas right-click context menu can reuse these exact
        QAction objects, same pattern as rotate_action/mirror_action/etc."""
        align_specs = [
            ("Align Left Edges", "left"),
            ("Align Right Edges", "right"),
            ("Align Top Edges", "top"),
            ("Align Bottom Edges", "bottom"),
            ("Align Horizontal Centers", "center_h"),
            ("Align Vertical Centers", "center_v"),
        ]
        self.align_actions = []
        for label, edge in align_specs:
            action = QAction(label, self)
            action.triggered.connect(lambda checked=False, e=edge: self._align_selected(e))
            self.align_actions.append(action)

        distribute_specs = [("Distribute Horizontally", "x"), ("Distribute Vertically", "y")]
        self.distribute_actions = []
        for label, axis in distribute_specs:
            action = QAction(label, self)
            action.triggered.connect(lambda checked=False, a=axis: self._distribute_selected(a))
            self.distribute_actions.append(action)

    def _align_selected(self, edge: str) -> None:
        """edge: one of 'left', 'right', 'top', 'bottom', 'center_h',
        'center_v'. 'top'/'bottom' refer to the visual screen direction
        (confirmed empirically: larger scene-y renders higher on screen,
        due to the canvas's global Y-flip), not QRectF's own top()/
        bottom() naming, which is the opposite — QRectF.top() is the
        *smaller* y, the visual bottom here."""
        boxes = self._selected_scene_bboxes()
        if len(boxes) < 2:
            return
        if edge == "left":
            target, axis, get_val = min(b.left() for b in boxes.values()), "x", lambda b: b.left()
        elif edge == "right":
            target, axis, get_val = max(b.right() for b in boxes.values()), "x", lambda b: b.right()
        elif edge == "top":
            target, axis, get_val = max(b.bottom() for b in boxes.values()), "y", lambda b: b.bottom()
        elif edge == "bottom":
            target, axis, get_val = min(b.top() for b in boxes.values()), "y", lambda b: b.top()
        elif edge == "center_h":
            axis, get_val = "x", lambda b: b.center().x()
            target = sum(b.center().x() for b in boxes.values()) / len(boxes)
        elif edge == "center_v":
            axis, get_val = "y", lambda b: b.center().y()
            target = sum(b.center().y() for b in boxes.values()) / len(boxes)
        else:
            raise ValueError(f"unknown align edge: {edge!r}")

        shifts = {inst_id: target - get_val(box) for inst_id, box in boxes.items()}
        self._apply_axis_shifts(shifts, axis, f"Align {edge.replace('_', ' ').title()}")

    def _distribute_selected(self, axis: str) -> None:
        """axis: 'x' (horizontal) or 'y' (vertical). Spaces instances'
        centers evenly between the extreme two (by current center
        position along that axis), which stay fixed — the standard
        "distribute centers" behavior in vector/CAD editors. Needs at
        least 3 instances to do anything (with 2, the "extremes" are the
        whole selection and nothing moves)."""
        boxes = self._selected_scene_bboxes()
        if len(boxes) < 3:
            return
        ordered = sorted(boxes.items(), key=lambda kv: kv[1].center().x() if axis == "x" else kv[1].center().y())
        first_center = ordered[0][1].center().x() if axis == "x" else ordered[0][1].center().y()
        last_center = ordered[-1][1].center().x() if axis == "x" else ordered[-1][1].center().y()
        step = (last_center - first_center) / (len(ordered) - 1)

        shifts = {}
        for i, (inst_id, box) in enumerate(ordered):
            current_center = box.center().x() if axis == "x" else box.center().y()
            target_center = first_center + step * i
            shifts[inst_id] = target_center - current_center
        self._apply_axis_shifts(shifts, axis, f"Distribute {'Horizontally' if axis == 'x' else 'Vertically'}")

    def _copy_selected(self) -> None:
        ids = self._selected_instance_ids()
        self._clipboard = [
            (self.document.instances[inst_id].component_spec, dict(self.document.instances[inst_id].kwargs))
            for inst_id in ids
        ]
        self.statusBar().showMessage(f"Copied {len(self._clipboard)} instance(s)", 2000)

    def _paste_clipboard(self) -> None:
        # Deliberately does NOT call undo_stack.undo() on a per-item failure
        # here (unlike _push_add_instance): tested calling undo() while a
        # macro is still open between beginMacro()/endMacro() and it produced
        # confusing, undocumented behavior (count() and callback ordering
        # didn't match a plain top-level undo at all). A failed paste is
        # left in the macro instead — harmless, since AddInstanceCommand's
        # .error guard already makes its redo()/undo() safe no-ops when
        # placement failed, just a redo-able inert entry in history.
        if not self._clipboard:
            return
        offset = self.view.grid_pitch * 2
        failures = []
        self.undo_stack.beginMacro("Paste")
        for component_spec, kwargs in self._clipboard:
            command = AddInstanceCommand(self.document, self.scene, component_spec, kwargs, x=offset, y=offset)
            self.undo_stack.push(command)
            if command.error is not None:
                failures.append(component_spec)
        self.undo_stack.endMacro()
        if failures:
            self.statusBar().showMessage(f"Could not paste: {', '.join(failures)}", 6000)

    def _select_all(self) -> None:
        for item in self.scene.items_by_inst.values():
            item.setSelected(True)
        for item in self.scene.route_items.values():
            item.setSelected(True)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _build_canvas_context_menu(self) -> QMenu:
        """Split from _show_canvas_context_menu so tests can exercise menu
        construction without calling the blocking QMenu.exec()."""
        menu = QMenu(self)
        menu.addAction(self.rotate_action)
        menu.addAction(self.mirror_action)
        menu.addAction(self.reset_transform_action)
        menu.addAction(self.delete_action)
        menu.addSeparator()
        align_menu = menu.addMenu("Align")
        for action in self.align_actions:
            align_menu.addAction(action)
        align_menu.addSeparator()
        for action in self.distribute_actions:
            align_menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.copy_action)
        menu.addAction(self.paste_action)
        menu.addSeparator()
        menu.addAction(self.select_all_action)
        menu.addAction(self.zoom_fit_action)
        menu.addAction(self.zoom_selection_action)
        return menu

    def _show_canvas_context_menu(self, pos: QPoint) -> None:
        menu = self._build_canvas_context_menu()
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _export_gds(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export GDS", "layout.gds", "GDS files (*.gds)")
        if not path:
            return
        try:
            written = self.document.export_gds(path)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export to {path}:\n{exc}")
            return
        self.statusBar().showMessage(f"Exported to {written}", 5000)

    def _export_python_script(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Python Script", "layout.py", "Python files (*.py)")
        if not path:
            return
        try:
            export_python_script(self.document, path)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export to {path}:\n{exc}")
            return
        self.statusBar().showMessage(f"Exported script to {path}", 5000)

    def _new_project(self) -> None:
        dialog = ProjectSettingsDialog(self.document.project_settings, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._reset_to_new_project(dialog.result_settings())

    def _reset_to_new_project(self, settings) -> None:
        """Split from _new_project so the actual reset logic is testable
        without the blocking modal dialog (same reasoning as
        _build_canvas_context_menu vs. _show_canvas_context_menu, and
        _apply_custom_components_file vs. _import_custom_components)."""
        inst_ids, route_ids = self.document.clear_all()
        for inst_id in inst_ids:
            self.scene.remove_instance_item(inst_id)
        for route_id in route_ids:
            self.scene.remove_route_item(route_id)
        self.scene.clear_reference_item()
        self.scene.clear_drc_violations()
        self.undo_stack.clear()
        self.project_path = None
        self._apply_project_settings(settings)
        self.statusBar().showMessage("New project", 2000)

    def _apply_project_settings(self, settings) -> None:
        self.document.project_settings = settings
        self.route_cross_section = settings.cross_section
        self.cross_section_combo.setCurrentText(settings.cross_section)

    def _edit_project_settings(self) -> None:
        dialog = ProjectSettingsDialog(self.document.project_settings, parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._apply_project_settings(dialog.result_settings())

    def _open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Project",
            "",
            "Phidler projects (*.phidler *.py);;Phidler project files (*.phidler);;Python scripts (*.py)",
        )
        if not path:
            return
        self._load_project_file(path)

    def _load_project_file(self, path: str) -> None:
        """Split from _open_project so the extension dispatch (.phidler vs
        .py) and the project_path safety behavior below are testable
        without the blocking QFileDialog call — same pattern as every
        other dialog-gated action in this app."""
        try:
            if path.endswith(".py"):
                # Best-effort, additive to .phidler: layer color/visibility
                # overrides and the reference GDS backdrop path have no
                # representation in the generated script and reset to
                # defaults — see import_script.py's module docstring.
                custom_specs = load_python_script(path, self.document, self.scene)
            else:
                custom_specs = load_project(path, self.document, self.scene)
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", f"Could not load {path}:\n{exc}")
            return
        if custom_specs:
            # re-registers this project's custom parts in the palette too —
            # they only existed in the active PDK's registry because the
            # import above just re-ran it for this fresh document
            for spec in custom_specs.values():
                self.catalog_by_name[spec.name] = spec
            self.catalog.setdefault("custom", []).extend(custom_specs.values())
            self.palette.add_components({"custom": list(custom_specs.values())})
        self.undo_stack.clear()
        # Deliberately NOT tracking project_path for a .py open: Save (Ctrl+S)
        # calls save_project(), which writes .phidler JSON — if project_path
        # pointed at the .py file, that would silently overwrite the user's
        # script with JSON content. Leaving it None forces the next Save
        # through Save As, where "project.phidler" is an explicit, visible
        # choice rather than a silent file-format swap.
        self.project_path = None if path.endswith(".py") else path
        self.statusBar().showMessage(f"Opened {path}", 3000)

    def _save_project(self) -> None:
        if self.project_path is None:
            self._save_project_as()
            return
        self._save_project_to(self.project_path)

    def _save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", "project.phidler", "Phidler projects (*.phidler)")
        if not path:
            return
        self._save_project_to(path)

    def _save_project_to(self, path: str) -> None:
        try:
            save_project(self.document, path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"Could not save to {path}:\n{exc}")
            return
        self.project_path = path
        self.statusBar().showMessage(f"Saved {path}", 3000)

    def _import_reference_gds(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Reference GDS", "", "GDS files (*.gds)")
        if not path:
            return
        try:
            self.document.import_reference(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", f"Could not import {path}:\n{exc}")
            return
        self.scene.show_reference()
        self.statusBar().showMessage(f"Imported reference {path}", 3000)

    def _clear_reference_gds(self) -> None:
        self.document.clear_reference()
        self.scene.clear_reference_item()

    def _import_custom_components(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Custom Components", "", "Python files (*.py)")
        if not path:
            return
        self._apply_custom_components_file(path)

    def _apply_custom_components_file(self, path: str) -> None:
        """Split from _import_custom_components so the load+merge logic is
        testable without the blocking QFileDialog call (the same reasoning
        as _build_canvas_context_menu vs. _show_canvas_context_menu)."""
        try:
            result = load_custom_components(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", f"Could not load components from {path}:\n{exc}")
            return
        if not result.specs:
            QMessageBox.warning(
                self,
                "No components found",
                f"{path} didn't contain any usable component factories "
                "(a callable with no required arguments that returns a gf.Component).",
            )
            return
        custom_specs = list(result.specs.values())
        for spec in custom_specs:
            self.catalog_by_name[spec.name] = spec
        self.catalog.setdefault("custom", []).extend(custom_specs)
        self.palette.add_components({"custom": custom_specs})
        self.document.record_custom_component_path(path)
        message = f"Imported {len(result.specs)} custom component(s) from {path}"
        if result.skipped:
            message += f" — skipped: {', '.join(result.skipped)}"
        self.statusBar().showMessage(message, 8000)

    def _on_instances_moved(self, inst_ids: list[int]) -> None:
        self.statusBar().showMessage(f"Moved instance(s): {inst_ids}", 2000)
