from phidler.main_window import MainWindow
from phidler.model.document import Transform
from phidler.pdk_catalog import list_cross_section_names


def test_cross_section_combo_populated_with_valid_pdk_names(qapp):
    win = MainWindow()
    names = [win.cross_section_combo.itemText(i) for i in range(win.cross_section_combo.count())]
    assert names == list_cross_section_names()
    assert "strip" in names
    assert win.cross_section_combo.currentText() == "strip"


def test_changing_cross_section_combo_updates_route_default(qapp):
    win = MainWindow()
    assert win.route_cross_section == "strip"
    win.cross_section_combo.setCurrentText("rib")
    assert win.route_cross_section == "rib"


def test_selected_cross_section_reaches_add_route(qapp):
    win = MainWindow()
    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win.document.set_transform(b_id, Transform(x=0.0, y=20.0, rotation=90.0, mirror=False))
    win.scene.items_by_inst[b_id].apply_transform(0.0, 20.0, 90.0, False)

    win.cross_section_combo.setCurrentText("rib")
    win._on_port_clicked(a_id, "o2")
    win._on_port_clicked(b_id, "o1")

    route_id = next(iter(win.document.routes))
    assert win.document.routes[route_id].cross_section == "rib"
