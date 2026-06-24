import math

from phidler.main_window import MainWindow
from phidler.model.document import Transform


def test_overlay_hidden_with_no_or_multiple_selection(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()

    win._update_transform_overlay()
    assert not win.transform_overlay.isVisible()

    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win.scene.items_by_inst[a_id].setSelected(True)
    win.scene.items_by_inst[b_id].setSelected(True)

    win._update_transform_overlay()
    assert not win.transform_overlay.isVisible()  # two selected — ambiguous, stays hidden


def test_overlay_shows_and_syncs_for_single_selection(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.show()  # QWidget.isVisible() reflects the whole ancestor chain —
    # view.show() alone leaves it False if MainWindow itself was never shown
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=0.0, y=0.0, rotation=45.0, mirror=True, mag=2.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win._update_transform_overlay()

    assert win.transform_overlay.isVisible()
    assert win.transform_overlay.rotation_slider.value() == 45
    assert win.transform_overlay.mirror_button.isChecked() is True
    assert win.transform_overlay.scale_slider.value() == 200


def test_rotate_buttons_preserve_mag_and_push_undoable_command(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False, mag=2.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.rotate_by_requested.emit(90.0)
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 90.0)
    assert math.isclose(t.mag, 2.0)

    win.undo_stack.undo()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 0.0)
    assert math.isclose(t.mag, 2.0)


def test_mirror_toggle_preserves_mag_and_rotation(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=0.0, y=0.0, rotation=30.0, mirror=False, mag=1.5))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.mirror_toggle_requested.emit()
    t = win.document.get_transform(inst_id)
    assert t.mirror is True
    assert math.isclose(t.rotation, 30.0)
    assert math.isclose(t.mag, 1.5)


def test_rotation_slider_live_preview_does_not_touch_document(qapp):
    """Live preview (dragging the slider) must only move the Qt item
    visually — the same "no model writes during interaction" pattern as
    drag-to-move — committing to the document/undo stack only on release."""
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.rotation_slider.setValue(123)  # fires valueChanged -> live preview

    item = win.scene.items_by_inst[inst_id]
    assert math.isclose(item.rotation_deg, 123.0)
    # the document/model must be untouched until commit
    assert math.isclose(win.document.get_transform(inst_id).rotation, 0.0)


def test_rotation_slider_commit_pushes_undoable_command(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.rotation_slider.setValue(200)
    win.transform_overlay.rotation_committed.emit(200.0)

    assert math.isclose(win.document.get_transform(inst_id).rotation, 200.0)
    win.undo_stack.undo()
    assert math.isclose(win.document.get_transform(inst_id).rotation, 0.0)


def test_scale_slider_live_preview_and_commit(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.scale_slider.setValue(250)  # live preview only
    item = win.scene.items_by_inst[inst_id]
    assert math.isclose(item.mag, 2.5)
    assert math.isclose(win.document.get_transform(inst_id).mag, 1.0)  # untouched until commit

    win.transform_overlay.scale_committed.emit(2.5)
    assert math.isclose(win.document.get_transform(inst_id).mag, 2.5)

    win.undo_stack.undo()
    assert math.isclose(win.document.get_transform(inst_id).mag, 1.0)


def test_reset_resets_rotation_mirror_and_scale_in_one_command(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=3.0, y=4.0, rotation=77.0, mirror=True, mag=3.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.reset_requested.emit()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 0.0)
    assert t.mirror is False
    assert math.isclose(t.mag, 1.0)
    assert math.isclose(t.x, 3.0) and math.isclose(t.y, 4.0)  # position untouched

    win.undo_stack.undo()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 77.0)
    assert t.mirror is True
    assert math.isclose(t.mag, 3.0)


def test_periodic_sync_skipped_while_user_is_dragging_a_slider(qapp):
    """The overlay's own values must not be clobbered by the periodic
    document-sync while the user is mid-drag on a slider — verified by
    forcing isSliderDown() (QSlider exposes setSliderDown for exactly this
    kind of test) rather than simulating a real mouse-drag sequence."""
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.transform_overlay.rotation_slider.setSliderDown(True)
    win.transform_overlay.rotation_slider.setValue(50)  # user mid-drag, not yet released

    win._update_transform_overlay()  # simulates a timer tick landing mid-drag

    assert win.transform_overlay.rotation_slider.value() == 50  # not reset back to 0 by the sync
