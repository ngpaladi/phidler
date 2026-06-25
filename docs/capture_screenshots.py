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

from pathlib import Path

from phidler.app import activate_pdk

activate_pdk()

from PySide6.QtWidgets import QApplication

app = QApplication([])

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
    win = MainWindow()
    win.resize(1500, 950)
    win.show()

    a = win.document.add_instance("ring_single", {"radius": 8.0, "gap": 0.3})
    win.scene.add_instance_item(a.id)

    b = win.document.add_instance("mmi1x2", {})
    win.scene.add_instance_item(b.id)
    win.document.set_transform(b.id, Transform(x=22.0, y=-2.0, rotation=0.0, mirror=False))

    win.view.zoom_to_fit()
    win.view.scale(0.85, 0.85)  # fitInView goes edge-to-edge; back off a bit
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
    dialog = ProjectSettingsDialog()
    dialog.resize(420, 320)
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
    a = win.document.add_instance("straight", {"length": 3.0, "width": 0.5})
    win.scene.add_instance_item(a.id)

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
    save(fdtd_win, "fdtd_propagation")


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


if __name__ == "__main__":
    main_overview()
    palette_with_hover_preview()
    transform_overlay()
    project_settings_dialog()
    drc_violation()
    fdtd_mode_profile()
    fdtd_propagation()
    console_session()
    print("done")
