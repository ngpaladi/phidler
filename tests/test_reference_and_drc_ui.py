from phidler.main_window import MainWindow


def _write_sample_gds(tmp_path):
    import gdsfactory as gf

    c = gf.Component()
    c.add_polygon([(0, 0), (5, 0), (5, 5), (0, 5)], layer=(2, 0))
    path = str(tmp_path / "reference.gds")
    c.write_gds(path)
    return path


def test_import_reference_renders_but_does_not_pollute_export(qapp, tmp_path):
    import gdsfactory as gf

    win = MainWindow()
    win._place_straight_waveguide()
    ref_path = _write_sample_gds(tmp_path)

    win.document.import_reference(ref_path)
    win.scene.show_reference()

    assert win.scene.reference_item is not None
    assert win.scene.reference_item.is_reference is True

    out_path = tmp_path / "export.gds"
    win.document.export_gds(str(out_path))
    reimported = gf.import_gds(str(out_path))
    polys = reimported.get_polygons(by="tuple")
    assert (2, 0) not in polys  # the reference's layer must not leak into export
    assert (1, 0) in polys  # the actual placed waveguide is still exported


def test_clear_reference_removes_item_and_document_state(qapp, tmp_path):
    win = MainWindow()
    ref_path = _write_sample_gds(tmp_path)
    win.document.import_reference(ref_path)
    win.scene.show_reference()

    win._clear_reference_gds()
    assert win.document.reference is None
    assert win.scene.reference_item is None


def test_new_project_clears_reference_too(qapp, tmp_path):
    win = MainWindow()
    win._place_straight_waveguide()
    ref_path = _write_sample_gds(tmp_path)
    win.document.import_reference(ref_path)
    win.scene.show_reference()

    from phidler.model.document import ProjectSettings

    win._reset_to_new_project(ProjectSettings())  # _new_project's dialog-free core
    assert win.document.reference is None
    assert win.scene.reference_item is None
    assert len(win.document.instances) == 0


def test_drc_panel_run_and_violation_click_centers_view(qapp):
    from phidler.model.layers import layer_info_for

    win = MainWindow()
    win.view.resize(400, 400)
    win.view.show()
    win.document.top.add_polygon([(0, 0), (10, 0), (10, 0.05), (0, 0.05)], layer=(1, 0))
    # document.layers only ever gets a layer key the first time normal
    # geometry extraction (add_instance/add_route/import_reference) sees
    # it; this test bypasses all of those to inject a precise violation
    # shape directly, so it must register the layer the same way they would.
    layer_info_for((1, 0), win.document.layers)
    win.drc_panel.set_layers(win.document.layers)

    win.drc_panel.width_spin.setValue(0.2)
    win.drc_panel.spacing_spin.setValue(0.0)
    assert win.drc_panel.set_current_layer((1, 0))

    win._on_run_drc((1, 0), 0.2, 0.0)
    assert len(win.drc_panel._violations) >= 1
    assert len(win.scene._violation_items) >= 1

    # Qt's QGraphicsView.centerOn() clamps to the scene's auto-computed
    # bounding rect, which is tiny here (just the sliver) vs. the 400x400
    # viewport, so reading the post-hoc center back is unreliable — assert
    # on the call itself instead of the resulting (Qt-clamped) viewport state.
    centered_on = []
    win.view.centerOn = lambda point: centered_on.append((point.x(), point.y()))

    v = win.drc_panel._violations[0]
    win._on_violation_selected(*v.bbox)

    assert len(centered_on) == 1
    expected_x = (v.bbox[0] + v.bbox[2]) / 2
    expected_y = (v.bbox[1] + v.bbox[3]) / 2
    assert abs(centered_on[0][0] - expected_x) < 1e-6
    assert abs(centered_on[0][1] - expected_y) < 1e-6
