"""Regenerates the screenshots embedded in README.md.

Builds a handful of representative app states and grabs them via
QWidget.grab(). This produces genuine rendered pixels even under
QT_QPA_PLATFORM=offscreen (confirmed: it's the real Qt paint pipeline,
just writing into an off-screen buffer instead of a window server) — not
mockups. What it can't capture is interactive feel; these are static
proof that each feature renders correctly, not that it's pleasant to use.

Run via the project's launcher so the PySide6/Qt library path fix is
applied (see run.sh / README's "Environment gotcha" section):

    QT_QPA_PLATFORM=offscreen ./run.sh -c "exec(open('docs/capture_screenshots.py').read())"

or, equivalently, from an already-activated venv with LD_LIBRARY_PATH set:

    QT_QPA_PLATFORM=offscreen python docs/capture_screenshots.py
"""

import os
from pathlib import Path

from phidler.app import activate_pdk

activate_pdk()

from PySide6.QtWidgets import QApplication

# Reuse an existing QApplication when imported (e.g. from the mkdocs build hook,
# or a second invocation under `mkdocs serve`); only create one when run as a
# standalone script. Constructing a second QApplication raises.
app = QApplication.instance() or QApplication([])

from phidler.main_window import MainWindow
from phidler.model.document import Transform
from phidler.model.layers import layer_info_for
from phidler.panels.project_settings_dialog import ProjectSettingsDialog

OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)


def save(widget, name: str) -> None:
    pixmap = widget.grab()
    path = OUT / f"{name}.png"
    pixmap.save(str(path))
    print(f"saved {path} ({pixmap.width()}x{pixmap.height()})")


def crop_top(name: str, fraction: float) -> None:
    """Keeps the top `fraction` of an already-saved screenshot (drops the
    bottom) — used for the mid-run propagation grab, where the field view below
    the progress bar is still empty (the movie is assembled only at the end)."""
    from PIL import Image

    path = OUT / f"{name}.png"
    img = Image.open(path)
    w, h = img.size
    img.crop((0, 0, w, int(h * fraction))).save(path)


def crop_bottom(name: str, fraction: float) -> None:
    """Crops the bottom `fraction` of an already-saved screenshot in
    place — used for the console screenshot, where the full window is
    mostly other panels and the interesting part is hard to read small."""
    from PIL import Image

    path = OUT / f"{name}.png"
    img = Image.open(path)
    w, h = img.size
    img.crop((0, int(h * (1 - fraction)), w, h)).save(path)


def main_overview() -> None:
    # A clean Mach-Zehnder interferometer: an mmi1x2 splitter, an mmi2x2
    # combiner, a straight reference arm and a length-matched delay arm that
    # meanders cleanly *below* it (no crossing waveguides).
    win = MainWindow()
    win.resize(1500, 820)
    win.show()

    splitter = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(splitter.id)
    combiner = win.document.add_instance("mmi2x2", {})
    win.scene.add_instance_item(combiner.id)
    win.document.set_transform(combiner.id, Transform(x=90.0, y=0.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[combiner.id].apply_transform(90.0, 0.0, 0.0, False)

    top = win.document.add_route(splitter.id, "o2", combiner.id, "o2", "strip")
    win.scene.add_route_item(top.id)
    bottom = win.document.add_route(
        splitter.id, "o3", combiner.id, "o1", "strip", goal_length_um=140.0, auto_match=True
    )
    win.scene.add_route_item(bottom.id)

    win.view.zoom_to_fit()
    win.view.scale(0.82, 0.82)  # fitInView goes edge-to-edge; back off a bit
    win.scene.clearSelection()
    save(win, "main_overview")


def palette_with_hover_preview() -> None:
    win = MainWindow()
    win.resize(420, 700)
    win.show()
    win.palette.resize(380, 680)

    tree = win.palette.tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if top.text(0).startswith("Rings"):
            leaf = top.child(0)
            tree.itemEntered.emit(leaf, 0)
            break
    save(win.palette, "palette_hover_preview")
    if win.palette._preview_popup.isVisible():
        save(win.palette._preview_popup, "hover_preview_popup")


def transform_overlay() -> None:
    win = MainWindow()
    win.resize(900, 700)
    win.show()
    inst = win.document.add_instance("bend_euler", {})
    win.scene.add_instance_item(inst.id)
    win.view.zoom_to_fit()
    win.scene.items_by_inst[inst.id].setSelected(True)
    win._update_transform_overlay()
    save(win, "transform_overlay")


def project_settings_dialog() -> None:
    from phidler.model.document import EtchLayer, ProjectSettings

    # Seed one etch/slab layer so the rib-waveguide control is visible (SLAB150
    # on layer 2, ~70 nm slab on standard SOI).
    dialog = ProjectSettingsDialog(initial=ProjectSettings(etch_layers=(EtchLayer(2, 0, 0.07),)))
    dialog.resize(440, 640)
    dialog.show()  # not exec() -- exec() blocks, there's no user here to dismiss it
    save(dialog, "project_settings_dialog")


def drc_violation() -> None:
    win = MainWindow()
    win.resize(1500, 950)
    win.show()
    win.document.top.add_polygon([(0, 0), (10, 0), (10, 0.05), (0, 0.05)], layer=(1, 0))
    layer_info_for((1, 0), win.document.layers)
    win.drc_panel.set_layers(win.document.layers)
    win.drc_panel.width_spin.setValue(0.2)
    win.drc_panel.spacing_spin.setValue(0.0)
    win.drc_panel.set_current_layer((1, 0))
    win._on_run_drc((1, 0), 0.2, 0.0)
    save(win, "drc_violation")


def _pump_events(app, predicate, max_iters: int = 300, sleep_s: float = 0.02) -> None:
    import time

    for _ in range(max_iters):
        app.processEvents()
        time.sleep(sleep_s)
        if predicate():
            return


def fdtd_mode_profile() -> None:
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    a = win.document.add_instance("straight", {"length": 3.0, "width": 0.5})
    win.scene.add_instance_item(a.id)

    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.resize(750, 950)
    fdtd_win.show()
    fdtd_win.centralWidget().setCurrentIndex(0)
    fdtd_win.mode_core_width_spin.setValue(0.5)
    fdtd_win._on_solve_mode_clicked()
    _pump_events(app, lambda: fdtd_win._mode_thread is not None and not fdtd_win._mode_thread.isRunning())
    save(fdtd_win, "fdtd_mode_profile")


def fdtd_propagation() -> None:
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    win.resize(900, 600)
    win.show()
    a = win.document.add_instance("straight", {"length": 3.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    # The propagation view copies the design canvas's viewport, so zoom the
    # design view onto the waveguide first or the field renders far off-screen.
    win.view.zoom_to_fit()
    win.view.scale(0.5, 0.5)

    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.resize(750, 950)
    fdtd_win.show()
    fdtd_win.centralWidget().setCurrentIndex(1)
    fdtd_win.run_cell_size_spin.setValue(0.06)
    fdtd_win.run_time_spin.setValue(20.0)
    fdtd_win._on_source_placement_requested(-0.4, 0.0)
    fdtd_win._on_run_clicked()
    _pump_events(app, lambda: fdtd_win._fdtd_thread is not None and not fdtd_win._fdtd_thread.isRunning())
    fdtd_win.frame_slider.setValue(fdtd_win.frame_slider.maximum())
    app.processEvents()
    fdtd_win.run_view.fit_to_image()  # frame the field + element outlines tightly
    app.processEvents()
    save(fdtd_win, "fdtd_propagation")


def fdtd_run_progress() -> None:
    """The Propagation (FDTD) tab during a run: the determinate progress bar
    filling, the "Running…" status, and the new "Run on remote server" row.
    Grabbed mid-solve from a real run (the field movie is only assembled at the
    end, so the view shows the chip outline the run is computing over)."""
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    win.resize(900, 600)
    win.show()
    a = win.document.add_instance("straight", {"length": 6.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.view.zoom_to_fit()
    win.view.scale(0.6, 0.6)

    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.resize(750, 950)
    fdtd_win.show()
    fdtd_win.centralWidget().setCurrentIndex(1)
    fdtd_win.run_cell_size_spin.setValue(0.07)
    fdtd_win.run_time_spin.setValue(40.0)  # enough steps to catch the bar partway
    fdtd_win._on_source_placement_requested(-0.4, 0.0)
    fdtd_win._on_run_clicked()

    # Stop pumping once the bar is determinate (first real tick) and at least a
    # third filled, while the worker is still running.
    def caught_midrun() -> bool:
        running = fdtd_win._fdtd_thread is not None and fdtd_win._fdtd_thread.isRunning()
        return (not running) or (
            fdtd_win.run_progress.maximum() == 100 and fdtd_win.run_progress.value() >= 35
        )

    _pump_events(app, caught_midrun)
    if fdtd_win._fdtd_thread is None or not fdtd_win._fdtd_thread.isRunning():
        # Solve finished before we sampled it (fast machine): put the bar at a
        # representative value so the screenshot still shows the in-run state.
        fdtd_win.run_progress.setVisible(True)
        fdtd_win.run_progress.setRange(0, 100)
        fdtd_win.run_progress.setValue(58)
        fdtd_win.run_status_label.setText("Running…")
    app.processEvents()
    save(fdtd_win, "fdtd_run_progress")
    # Drop the still-empty field view below the bar; keep controls + progress.
    crop_top("fdtd_run_progress", 0.72)
    # Let the worker finish so the next capture doesn't tear down a live thread.
    _pump_events(app, lambda: fdtd_win._fdtd_thread is None or not fdtd_win._fdtd_thread.isRunning())


def remote_config_dialog() -> None:
    """The remote-server setup dialog: host alias, remote dir + Python, the
    GPU-on-remote toggle, and the Test connection / Set up remote actions."""
    from phidler.panels.fdtd_window import RemoteConfigDialog

    dialog = RemoteConfigDialog()
    dialog.resize(620, 460)
    dialog.alias_edit.setText("gpubox")
    dialog.remote_dir_edit.setText("~/phidler-remote")
    dialog.remote_python_edit.setText("~/phidler-remote/.venv/bin/python")
    dialog.use_gpu_check.setChecked(True)
    # The exact line check_remote emits on success, so the log pane shows a
    # realistic result rather than the empty placeholder.
    dialog._append("Connected to 'gpubox': phidler and photonfdtd import successfully.")
    dialog.show()
    save(dialog, "remote_config_dialog")


def routing_example() -> None:
    from PySide6.QtCore import QPointF

    win = MainWindow()
    win.resize(1100, 700)
    win.show()

    a = win.document.add_instance("straight", {"length": 8.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.document.set_transform(a.id, Transform(x=-8.0, y=0.0, rotation=0.0, mirror=False))

    b = win.document.add_instance("bend_euler", {})
    win.scene.add_instance_item(b.id)
    win.document.set_transform(b.id, Transform(x=4.0, y=0.0, rotation=0.0, mirror=False))

    route = win.document.add_route(a.id, "o2", b.id, "o1", "strip")
    win.scene.add_route_item(route.id)

    win.view.zoom_to_fit()
    win.view.scale(0.8, 0.8)
    win.scene.clearSelection()
    win.route_action.setChecked(True)
    save(win, "routing_example")
    win.route_action.setChecked(False)


def measure_tool_example() -> None:
    from PySide6.QtCore import QPointF

    win = MainWindow()
    win.resize(1100, 700)
    win.show()

    a = win.document.add_instance("straight", {"length": 12.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.document.set_transform(a.id, Transform(x=-6.0, y=0.0, rotation=0.0, mirror=False))

    b = win.document.add_instance("bend_euler", {})
    win.scene.add_instance_item(b.id)
    win.document.set_transform(b.id, Transform(x=6.0, y=0.0, rotation=0.0, mirror=False))

    win.view.zoom_to_fit()
    win.view.scale(0.75, 0.75)
    win.view.set_measure_mode(True)
    win.view._draw_measurement(
        QPointF(-6.0, 0.0), QPointF(6.0, 0.0),
        dx=12.0, dy=0.0, distance=12.0,
    )
    save(win, "measure_tool_example")
    win.view.set_measure_mode(False)


def layers_panel_example() -> None:
    win = MainWindow()
    win.resize(350, 520)
    win.show()

    # A spread of components that between them carry a representative variety of
    # layer types — waveguide (WG), a half-etch slab (SLAB150 under the grating),
    # and a heater with its metal routing and vias — so the panel illustrates the
    # layer-types reference in the guide rather than just the waveguide layer.
    for comp, kwargs, x in [
        ("straight", {"length": 10.0, "width": 0.5}, 0.0),
        ("grating_coupler_elliptical_te", {}, 18.0),
        ("straight_heater_metal_simple", {"length": 10.0}, 40.0),
    ]:
        inst = win.document.add_instance(comp, kwargs)
        win.scene.add_instance_item(inst.id)
        win.document.set_transform(inst.id, Transform(x=x, y=0.0, rotation=0.0, mirror=False))

    win.undo_stack.indexChanged.emit(0)
    # The panel's natural size only shows a few rows before scrolling; grab it
    # tall enough to show the whole layer list.
    win.layers_panel.resize(300, 235)
    app.processEvents()
    save(win.layers_panel, "layers_panel_example")


def properties_panel_example() -> None:
    win = MainWindow()
    win.resize(320, 600)
    win.show()

    inst = win.document.add_instance("ring_single", {"radius": 8.0, "gap": 0.3})
    win.scene.add_instance_item(inst.id)
    win.scene.items_by_inst[inst.id].setSelected(True)
    win.scene.selectionChanged.emit()
    # The panel scrolls (small minimum size), so grab it at a height that shows
    # the whole form rather than its collapsed minimum.
    win.properties_panel.resize(300, 560)
    app.processEvents()
    save(win.properties_panel, "properties_panel_example")


def console_session() -> None:
    win = MainWindow()
    win.resize(1200, 800)
    win.show()

    def submit(line: str) -> None:
        win.console_panel.input.setText(line)
        win.console_panel._on_return()

    submit("a = place('straight', length=12.0, x=0.0, y=0.0)")
    submit("b = place('bend_euler', x=15.0, y=0.0, rotation=90.0)")
    submit("route(a.id, 'o2', b.id, 'o1')")
    submit("print(f'{len(doc.instances)} instances, {len(doc.routes)} routes')")
    win.view.zoom_to_fit()
    save(win, "console_session")
    crop_bottom("console_session", fraction=0.38)


def _mzi_window(with_delay: bool = True):
    """A Mach-Zehnder interferometer: one mmi1x2 splitter, one mmi2x2
    combiner, two arms. The lower arm is length-matched to a longer target so
    it picks up a meander delay (used by the tutorial to show goal-length
    routing)."""
    win = MainWindow()
    win.resize(1280, 560)
    win.show()

    splitter = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(splitter.id)

    combiner = win.document.add_instance("mmi2x2", {})
    win.scene.add_instance_item(combiner.id)
    win.document.set_transform(combiner.id, Transform(x=90.0, y=0.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[combiner.id].apply_transform(90.0, 0.0, 0.0, False)

    top = win.document.add_route(splitter.id, "o2", combiner.id, "o2", "strip")
    win.scene.add_route_item(top.id)

    bottom = win.document.add_route(
        splitter.id,
        "o3",
        combiner.id,
        "o1",
        "strip",
        goal_length_um=140.0 if with_delay else None,
        auto_match=with_delay,
    )
    win.scene.add_route_item(bottom.id)

    win.view.zoom_to_fit()
    win.view.scale(1.15, 1.15)  # fill the canvas; the meander gives it vertical extent
    win.scene.clearSelection()
    return win, splitter, combiner, top, bottom


def tutorial_mzi_components() -> None:
    win = MainWindow()
    win.resize(1280, 560)
    win.show()
    splitter = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(splitter.id)
    combiner = win.document.add_instance("mmi2x2", {})
    win.scene.add_instance_item(combiner.id)
    win.document.set_transform(combiner.id, Transform(x=90.0, y=0.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[combiner.id].apply_transform(90.0, 0.0, 0.0, False)
    win.view.zoom_to_fit()
    win.view.scale(1.1, 1.1)
    win.scene.clearSelection()
    save(win, "tutorial_mzi_components")


def tutorial_mzi_routed() -> None:
    win, _s, _c, _t, _b = _mzi_window(with_delay=True)
    save(win, "tutorial_mzi_routed")


def tutorial_mzi_delay_readout() -> None:
    win, _s, _c, _top, bottom = _mzi_window(with_delay=True)
    win.scene.route_items[bottom.id].setSelected(True)
    win._show_route_readout()
    save(win, "tutorial_mzi_delay_readout")


def tutorial_routing_feedback() -> None:
    """The routing hover-highlight + rubber-band preview track after the first
    port is picked."""
    from PySide6.QtCore import QPointF

    win = MainWindow()
    win.resize(1000, 600)
    win.show()
    a = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("bend_euler", {})
    win.scene.add_instance_item(b.id)
    win.document.set_transform(b.id, Transform(x=45.0, y=-8.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[b.id].apply_transform(45.0, -8.0, 0.0, False)
    win.view.zoom_to_fit()
    win.view.scale(0.8, 0.8)
    win.scene.clearSelection()
    win.route_action.setChecked(True)
    # Pick the splitter's o2, then hover near the bend's o1 so both the
    # highlight and the preview track are showing.
    win._on_port_clicked(a.id, "o2")
    o1 = win.view._port_scene_pos(b.id, "o1")
    win.view._update_hover_port(o1)
    win.view._update_route_preview(o1)
    save(win, "tutorial_routing_feedback")
    win.route_action.setChecked(False)


# ---------------------------------------------------------------------------
# Expanded gallery captures (feature tour). These favour rich, real app states
# over UI chrome. Menus/combo popups grab correctly under offscreen Qt (they
# render into the same off-screen buffer once popped up); native OS file
# dialogs do not, so they're deliberately absent.
# ---------------------------------------------------------------------------


def _grab_menu(menu, name: str) -> None:
    """Pop a QMenu up (menus only lay out once shown) and grab it."""
    from PySide6.QtCore import QPoint

    menu.popup(QPoint(0, 0))
    app.processEvents()
    save(menu, name)
    menu.close()


def _menu_by_title(win, title: str):
    from PySide6.QtWidgets import QMenu

    return next(m for m in win.menuBar().findChildren(QMenu) if m.title() == title)


def _grab_combo_popup(combo, name: str) -> None:
    """Open a QComboBox dropdown and grab the popup (a separate top-level view
    that still renders off-screen)."""
    combo.showPopup()
    app.processEvents()
    view = combo.view()
    save(view.parentWidget() or view, name)
    combo.hidePopup()


def _demo_window(width: int = 1400, height: int = 820):
    win = MainWindow()
    win.resize(width, height)
    win.show()
    return win


def menu_file() -> None:
    win = _demo_window()
    _grab_menu(_menu_by_title(win, "&File"), "menu_file")


def menu_edit() -> None:
    win = _demo_window()
    # Populate history + a selection so the menu shows its actions live, not
    # greyed: place, select, and rotate once (enables Undo).
    inst = win.document.add_instance("bend_euler", {})
    win.scene.add_instance_item(inst.id)
    win.scene.items_by_inst[inst.id].setSelected(True)
    win._rotate_selected()
    _grab_menu(_menu_by_title(win, "&Edit"), "menu_edit")


def menu_view() -> None:
    win = _demo_window()
    _grab_menu(_menu_by_title(win, "&View"), "menu_view")


def context_menu() -> None:
    """The right-click canvas menu (rotate/flip/align/copy/delete/zoom) over a
    selected instance."""
    win = _demo_window(1200, 760)
    inst = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(inst.id)
    win.view.zoom_to_fit()
    win.view.scale(0.8, 0.8)
    win.scene.items_by_inst[inst.id].setSelected(True)
    win._update_transform_overlay()
    _grab_menu(win._build_canvas_context_menu(), "context_menu")


def multi_select() -> None:
    """Two components selected together — the shared selection highlight the
    align/distribute and group-transform tools act on."""
    win = _demo_window(1100, 700)
    a = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(a.id)
    b = win.document.add_instance("ring_single", {"radius": 6.0})
    win.scene.add_instance_item(b.id)
    win.document.set_transform(b.id, Transform(x=40.0, y=-18.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[b.id].apply_transform(40.0, -18.0, 0.0, False)
    win.view.zoom_to_fit()
    win.view.scale(0.85, 0.85)
    for inst_id in (a.id, b.id):
        win.scene.items_by_inst[inst_id].setSelected(True)
    win._update_transform_overlay()
    save(win, "multi_select")


def array_layout() -> None:
    """One component tiled into an array (a fiber grating-coupler bank) — set
    once in the Array section of the properties panel, placed as a unit."""
    from phidler.model.placed_instance import ArraySpec

    win = _demo_window(1100, 760)
    inst = win.document.add_instance(
        "grating_coupler_elliptical_te", {}, array=ArraySpec(columns=1, rows=6, row_pitch=25.0)
    )
    win.scene.add_instance_item(inst.id)
    win.view.zoom_to_fit()
    win.view.scale(0.85, 0.85)
    win.scene.clearSelection()
    save(win, "array_layout")


def distribute_result() -> None:
    """Three components spaced evenly by Distribute Horizontally — scattered
    input, even output."""
    win = _demo_window(1200, 640)
    xs = (-30.0, -2.0, 34.0)  # deliberately uneven gaps
    ids = []
    for x in xs:
        inst = win.document.add_instance("ring_single", {"radius": 5.0})
        win.scene.add_instance_item(inst.id)
        win.document.set_transform(inst.id, Transform(x=x, y=0.0, rotation=0.0, mirror=False))
        win.scene.items_by_inst[inst.id].apply_transform(x, 0.0, 0.0, False)
        ids.append(inst.id)
    for inst_id in ids:
        win.scene.items_by_inst[inst_id].setSelected(True)
    win._distribute_selected("x")
    win.view.zoom_to_fit()
    win.view.scale(0.9, 0.9)
    save(win, "distribute_result")


def reference_overlay() -> None:
    """A design drawn on top of a dimmed reference GDS backdrop — the trace /
    align-to-existing-layout workflow."""
    import tempfile

    import gdsfactory as gf

    ref_path = Path(tempfile.gettempdir()) / "phidler_ref_demo.gds"
    gf.components.ring_double().write_gds(str(ref_path))

    win = _demo_window(1100, 720)
    win.document.import_reference(str(ref_path))
    win.scene.show_reference()
    # A new element being drawn over the backdrop.
    inst = win.document.add_instance("straight", {"length": 12.0, "width": 0.5})
    win.scene.add_instance_item(inst.id)
    win.document.set_transform(inst.id, Transform(x=-6.0, y=8.0, rotation=0.0, mirror=False))
    win.scene.items_by_inst[inst.id].apply_transform(-6.0, 8.0, 0.0, False)
    win.view.zoom_to_fit()
    win.view.scale(0.8, 0.8)
    win.scene.clearSelection()
    save(win, "reference_overlay")


def showcase_ring() -> None:
    """A clean add–drop ring resonator — a recognisable device, framed as a
    gallery hero."""
    win = _demo_window(1100, 720)
    inst = win.document.add_instance("ring_double", {})
    win.scene.add_instance_item(inst.id)
    win.view.zoom_to_fit()
    win.view.scale(0.85, 0.85)
    win.scene.clearSelection()
    save(win, "showcase_ring")


def cross_section_dropdown() -> None:
    """The routing cross-section picker open — every PDK cross-section a route
    can be drawn with."""
    win = _demo_window()
    _grab_combo_popup(win.cross_section_combo, "cross_section_dropdown")


def palette_search() -> None:
    """The component palette filtered live by a search term."""
    win = MainWindow()
    win.resize(420, 720)
    win.show()
    win.palette.resize(380, 700)
    win.palette.search_box.setText("ring")
    app.processEvents()
    win.palette.tree.expandAll()
    app.processEvents()
    save(win.palette, "palette_search")


def drc_panel_results() -> None:
    """The DRC panel after a check: min-width / min-spacing controls and a
    violation flagged in the results list."""
    win = _demo_window(1400, 900)
    win.document.top.add_polygon([(0, 0), (10, 0), (10, 0.05), (0, 0.05)], layer=(1, 0))
    layer_info_for((1, 0), win.document.layers)
    win.drc_panel.set_layers(win.document.layers)
    win.drc_panel.width_spin.setValue(0.2)
    win.drc_panel.spacing_spin.setValue(0.0)
    win.drc_panel.set_current_layer((1, 0))
    win._on_run_drc((1, 0), 0.2, 0.0)
    win.drc_panel.resize(320, 380)
    app.processEvents()
    save(win.drc_panel, "drc_panel_results")


def _fdtd_prop_window_with_sources():
    """A propagation-tab FDTD window with two sources of different kinds placed —
    shared by the source-table and dropdown captures so they don't each rebuild."""
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    a = win.document.add_instance("straight", {"length": 6.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.view.zoom_to_fit()
    win.view.scale(0.5, 0.5)

    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.resize(760, 960)
    fdtd_win.show()
    fdtd_win.centralWidget().setCurrentIndex(1)  # Propagation tab
    fdtd_win._on_source_placement_requested(-1.5, 0.0)
    fdtd_win._on_source_placement_requested(1.5, 0.4)
    from phidler.panels.fdtd_window import _COL_KIND

    fdtd_win.source_table.cellWidget(1, _COL_KIND).setCurrentText("cherenkov")
    app.processEvents()
    return win, fdtd_win


def fdtd_source_setup() -> None:
    win, fdtd_win = _fdtd_prop_window_with_sources()
    save(fdtd_win, "fdtd_source_setup")
    crop_top("fdtd_source_setup", 0.62)  # controls + populated source table


def fdtd_source_kind_dropdown() -> None:
    from phidler.panels.fdtd_window import _COL_KIND

    win, fdtd_win = _fdtd_prop_window_with_sources()
    _grab_combo_popup(fdtd_win.source_table.cellWidget(0, _COL_KIND), "fdtd_source_kind_dropdown")


def fdtd_cladding_material_dropdown() -> None:
    win, fdtd_win = _fdtd_prop_window_with_sources()
    _grab_combo_popup(fdtd_win.run_clad_row._combo, "fdtd_cladding_material_dropdown")


def fdtd_field_midframe() -> None:
    """A propagation snapshot caught mid-flight (the pulse partway across the
    waveguide) rather than at the end — the most eye-catching FDTD view."""
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    win.resize(900, 600)
    win.show()
    a = win.document.add_instance("straight", {"length": 6.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.view.zoom_to_fit()
    win.view.scale(0.5, 0.5)

    fdtd_win = FdtdWindow(win.document, win.view)
    fdtd_win.resize(750, 950)
    fdtd_win.show()
    fdtd_win.centralWidget().setCurrentIndex(1)
    fdtd_win.run_cell_size_spin.setValue(0.06)
    fdtd_win.run_time_spin.setValue(30.0)
    fdtd_win._on_source_placement_requested(-2.2, 0.0)
    fdtd_win._on_run_clicked()
    _pump_events(app, lambda: fdtd_win._fdtd_thread is not None and not fdtd_win._fdtd_thread.isRunning())
    # ~40% through the movie: the pulse is mid-waveguide, both ends still visible.
    fdtd_win.frame_slider.setValue(int(fdtd_win.frame_slider.maximum() * 0.4))
    app.processEvents()
    fdtd_win.run_view.fit_to_image()
    app.processEvents()
    save(fdtd_win, "fdtd_field_midframe")


def startup_dialog() -> None:
    """The launcher's recent-projects dialog — the first thing you see."""
    from phidler.panels.startup_dialog import StartupDialog

    recents = [
        "/home/you/photonics/mzi_filter.phidler",
        "/home/you/photonics/ring_modulator.phidler",
        "/home/you/photonics/grating_coupler_array.phidler",
    ]
    dialog = StartupDialog(recents)
    dialog.resize(480, 360)
    dialog.show()
    dialog.list.setCurrentRow(0)  # selects a project so Open Selected is live
    app.processEvents()
    save(dialog, "startup_dialog")


def units_time_view() -> None:
    """Coordinate axes shown as propagation time (fs) instead of microns — the
    time-of-flight view for delay-line design."""
    win = _demo_window(1100, 700)
    a = win.document.add_instance("straight", {"length": 40.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.view.zoom_to_fit()
    win.view.scale(0.8, 0.8)
    win.scene.clearSelection()
    win.units_combo.setCurrentIndex(2)  # "fs  (propagation time)"
    app.processEvents()
    save(win, "units_time_view")


def grid_snap_closeup() -> None:
    """The snapping grid up close — placement, dragging, and routing round to
    this pitch when Snap is on."""
    win = _demo_window(1000, 700)
    a = win.document.add_instance("straight", {"length": 6.0, "width": 0.5})
    win.scene.add_instance_item(a.id)
    win.view.zoom_to_fit()
    win.view.scale(3.0, 3.0)  # zoom in until the 1 µm grid squares are visible
    win.scene.clearSelection()
    save(win, "grid_snap_closeup")


def align_result() -> None:
    """Three components snapped to a common top edge with Align."""
    win = _demo_window(1200, 640)
    ids = []
    for i, y in enumerate((10.0, -6.0, 2.0)):
        inst = win.document.add_instance("mmi1x2", {})
        win.scene.add_instance_item(inst.id)
        x = -40.0 + i * 40.0
        win.document.set_transform(inst.id, Transform(x=x, y=y, rotation=0.0, mirror=False))
        win.scene.items_by_inst[inst.id].apply_transform(x, y, 0.0, False)
        ids.append(inst.id)
    for inst_id in ids:
        win.scene.items_by_inst[inst_id].setSelected(True)
    win._align_selected("top")
    win.view.zoom_to_fit()
    win.view.scale(0.9, 0.9)
    save(win, "align_result")


def heater_showcase() -> None:
    """A thermo-optic phase shifter — a waveguide with a metal heater and its
    routing — showing the metal and via layers stacked over the optical layer."""
    win = _demo_window(1100, 680)
    inst = win.document.add_instance("straight_heater_metal_simple", {"length": 40.0})
    win.scene.add_instance_item(inst.id)
    win.undo_stack.indexChanged.emit(0)  # refresh the Layers panel so the stack shows
    win.view.zoom_to_fit()
    win.view.scale(0.85, 0.85)
    win.scene.clearSelection()
    app.processEvents()
    save(win, "heater_showcase")


def palette_catalog() -> None:
    """The component palette with its top categories expanded — the gdsfactory
    PDK catalog, grouped by kind."""
    win = MainWindow()
    win.resize(420, 900)
    win.show()
    win.palette.resize(380, 880)
    tree = win.palette.tree
    for i in range(min(tree.topLevelItemCount(), 4)):
        tree.topLevelItem(i).setExpanded(True)
    app.processEvents()
    save(win.palette, "palette_catalog")


def regenerate_all() -> None:
    """Rebuild every embedded screenshot. Called by the standalone script and
    by the mkdocs pre-build hook (docs/hooks.py)."""
    tutorial_mzi_components()
    tutorial_mzi_routed()
    tutorial_mzi_delay_readout()
    tutorial_routing_feedback()
    main_overview()
    palette_with_hover_preview()
    transform_overlay()
    project_settings_dialog()
    drc_violation()
    fdtd_mode_profile()
    fdtd_propagation()
    fdtd_run_progress()
    remote_config_dialog()
    routing_example()
    measure_tool_example()
    layers_panel_example()
    properties_panel_example()
    console_session()
    # Expanded feature-tour gallery
    menu_file()
    menu_edit()
    menu_view()
    context_menu()
    multi_select()
    array_layout()
    distribute_result()
    reference_overlay()
    showcase_ring()
    cross_section_dropdown()
    palette_search()
    drc_panel_results()
    fdtd_source_setup()
    fdtd_source_kind_dropdown()
    fdtd_cladding_material_dropdown()
    fdtd_field_midframe()
    startup_dialog()
    units_time_view()
    grid_snap_closeup()
    align_result()
    heater_showcase()
    palette_catalog()
    print("done")


if __name__ == "__main__":
    regenerate_all()
