import math

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox

from phidler.main_window import MainWindow
from phidler.pdk_catalog import list_cross_section_names


def test_palette_arm_and_click_places(qapp):
    """At the MainWindow/view level only — exercises arm->click->place
    via a direct place_requested emission, not the palette's actual click
    semantics (now a single click; see test_component_palette.py for
    that). What's a single click vs. double-click at the palette doesn't
    matter here, only that arming followed by a canvas click places."""
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()

    win.palette.place_requested.emit("straight")
    assert win.view.armed_component == "straight"

    view_pt = win.view.mapFromScene(5.0, 5.0)
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, view_pt)

    assert win.view.armed_component is None  # one-shot
    assert len(win.document.instances) == 1
    inst_id = next(iter(win.document.instances))
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.x, 5.0, abs_tol=1e-9)
    assert math.isclose(t.y, 5.0, abs_tol=1e-9)


def test_escape_cancels_armed_placement(qapp):
    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()

    win.view.arm_placement("straight")
    QTest.keyClick(win.view, Qt.Key_Escape)
    assert win.view.armed_component is None
    assert len(win.document.instances) == 0


def test_selecting_instance_populates_properties_panel(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))

    win.scene.items_by_inst[inst_id].setSelected(True)

    assert win.properties_panel._inst_id == inst_id
    assert "length" in win.properties_panel._fields


def test_editing_property_pushes_undoable_command(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    length_field = win.properties_panel._fields["length"]
    length_field.setValue(25.0)
    win.properties_panel._on_apply()

    assert win.document.instances[inst_id].kwargs["length"] == 25.0
    shapes = win.document.get_polygons_for_instance(inst_id)
    xs = [x for hull, _holes in shapes[(1, 0)] for x, _ in hull]
    assert math.isclose(max(xs), 25.0, abs_tol=1e-6)

    win.undo_stack.undo()
    assert win.document.instances[inst_id].kwargs["length"] == 10.0


def test_invalid_property_edit_does_not_corrupt_instance(qapp):
    """An invalid cross_section name makes gf.get_component raise. The
    instance must survive untouched (still in top.insts, ref still valid),
    not be left half-deleted: gone from the GDS topology but still tracked
    by the document as if it existed."""
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    n_insts_before = len(list(win.document.top.insts))

    win._on_params_applied(inst_id, {"length": 10.0, "width": 0.5, "cross_section": "NOT_A_REAL_CROSS_SECTION"})

    assert inst_id in win.document.instances
    assert len(list(win.document.top.insts)) == n_insts_before
    # the ref must still be a live, queryable part of the layout
    win.document.get_transform(inst_id)
    win.document.get_polygons_for_instance(inst_id)

    # the failed edit must not have left a poisoned entry that corrupts
    # state on undo/redo
    win.undo_stack.undo()
    win.undo_stack.redo()
    assert inst_id in win.document.instances
    assert len(list(win.document.top.insts)) == n_insts_before


def test_cross_section_field_is_dropdown_with_valid_options(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    field = win.properties_panel._fields["cross_section"]
    assert isinstance(field, QComboBox)
    options = [field.itemText(i) for i in range(field.count())]
    assert options == list_cross_section_names()
    assert field.currentText() == "strip"


def test_changing_cross_section_dropdown_and_applying_regenerates_geometry(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    field = win.properties_panel._fields["cross_section"]
    field.setCurrentText("rib")
    win.properties_panel._on_apply()

    assert win.document.instances[inst_id].kwargs["cross_section"] == "rib"


def test_deselecting_clears_properties_panel(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)
    assert win.properties_panel._inst_id == inst_id

    win.scene.clearSelection()
    assert win.properties_panel._inst_id is None


def test_selecting_instance_populates_transform_fields(qapp):
    from phidler.model.document import Transform

    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.document.set_transform(inst_id, Transform(x=5.0, y=3.0, rotation=45.0, mirror=True, mag=2.0))
    win.scene.items_by_inst[inst_id].setSelected(True)

    panel = win.properties_panel
    assert math.isclose(panel.x_spin.value(), 5.0)
    assert math.isclose(panel.y_spin.value(), 3.0)
    assert math.isclose(panel.rotation_spin.value(), 45.0)
    assert panel.mirror_check.isChecked() is True
    assert math.isclose(panel.scale_spin.value(), 2.0)


def test_editing_transform_fields_and_applying_pushes_undoable_command(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    panel = win.properties_panel
    panel.x_spin.setValue(12.5)
    panel.y_spin.setValue(-4.0)
    panel.rotation_spin.setValue(90.0)
    panel.mirror_check.setChecked(True)
    panel.scale_spin.setValue(1.5)
    panel._on_apply_transform()

    t = win.document.get_transform(inst_id)
    assert math.isclose(t.x, 12.5)
    assert math.isclose(t.y, -4.0)
    assert math.isclose(t.rotation, 90.0)
    assert t.mirror is True
    assert math.isclose(t.mag, 1.5)

    win.undo_stack.undo()
    t = win.document.get_transform(inst_id)
    assert math.isclose(t.x, 0.0)
    assert math.isclose(t.mag, 1.0)


def test_applying_negative_rotation_normalizes_to_0_360_range(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    panel = win.properties_panel
    panel.rotation_spin.setValue(-90.0)
    panel._on_apply_transform()

    t = win.document.get_transform(inst_id)
    assert math.isclose(t.rotation, 270.0)


def test_applying_nonpositive_scale_is_clamped_to_a_small_positive_value(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    panel = win.properties_panel
    panel.scale_spin.setValue(0.0)
    panel._on_apply_transform()

    t = win.document.get_transform(inst_id)
    assert t.mag > 0.0


def test_periodic_sync_does_not_clobber_a_field_the_user_is_editing(qapp, monkeypatch):
    """The same is-interacting-style guard the transform handles use for
    their own periodic resync — update_transform() must skip the sync
    entirely while the user is editing one of the transform fields, or
    typing a new X value would get overwritten mid-edit by the next timer
    tick reading the old, not-yet-applied document value.

    Real Qt focus (QWidget.hasFocus()) never becomes true under
    QT_QPA_PLATFORM=offscreen — confirmed empirically across several
    setFocus()/activateWindow() combinations, since there's no real
    window manager to grant input focus headlessly. So this tests the
    guard's actual decision logic by monkeypatching _is_editing_transform
    directly, rather than the real focus plumbing that can't be exercised
    this way in this environment."""
    win = MainWindow()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    monkeypatch.setattr(win.properties_panel, "_is_editing_transform", lambda: True)
    win.properties_panel.x_spin.setValue(42.0)
    win._update_transform_overlay()  # simulates a timer tick while "editing"

    assert math.isclose(win.properties_panel.x_spin.value(), 42.0)


def test_periodic_sync_updates_fields_when_not_editing(qapp):
    from phidler.model.document import Transform

    win = MainWindow()
    win.view.resize(400, 400)
    win.show()
    win._place_straight_waveguide()
    inst_id = next(iter(win.document.instances))
    win.scene.items_by_inst[inst_id].setSelected(True)

    win.document.set_transform(inst_id, Transform(x=7.0, y=8.0, rotation=0.0, mirror=False, mag=1.0))
    win._update_transform_overlay()

    assert math.isclose(win.properties_panel.x_spin.value(), 7.0)
    assert math.isclose(win.properties_panel.y_spin.value(), 8.0)
