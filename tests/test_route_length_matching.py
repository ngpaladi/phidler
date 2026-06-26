"""Routed-trace goal length: manual (report only) and automatic (insert an
adiabatic euler-bend meander to approach the target)."""

import math

from phidler.canvas.scene import LayoutScene
from phidler.model.document import LayoutDocument
from phidler.project_io import load_project, save_project


def _two_straights_in_a_line(doc, gap_x=100.0):
    a = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 10.0, "width": 0.5}, x=gap_x, y=0.0)
    return a, b


def _len_um(doc, route):
    return route.length * doc.top.kcl.dbu


def test_automatic_meander_converges_near_the_goal(qapp):
    doc = LayoutDocument()
    a, b = _two_straights_in_a_line(doc)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", goal_length_um=200.0, auto_match=True)
    actual = _len_um(doc, route)
    assert route.meander_amplitude_um is not None  # a meander was inserted
    assert abs(actual - 200.0) < 2.0  # within a couple µm of the target


def test_manual_mode_records_goal_but_routes_directly(qapp):
    doc = LayoutDocument()
    a, b = _two_straights_in_a_line(doc)
    natural = doc.add_route(a.id, "o2", b.id, "o1", "strip")
    natural_len = _len_um(doc, natural)
    doc.remove_route(natural.id)

    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", goal_length_um=200.0, auto_match=False)
    assert route.goal_length_um == 200.0
    assert route.meander_amplitude_um is None  # manual: no meander inserted
    assert math.isclose(_len_um(doc, route), natural_len, abs_tol=1e-6)


def test_goal_shorter_than_natural_uses_the_natural_route(qapp):
    doc = LayoutDocument()
    a, b = _two_straights_in_a_line(doc)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", goal_length_um=10.0, auto_match=True)
    assert route.meander_amplitude_um is None  # can't shorten below the direct route


def test_auto_matched_route_reload_is_deterministic(qapp, tmp_path):
    """project_io replays the recipe, so a reloaded auto-matched route must
    rebuild the *same* geometry — driven by the persisted solved amplitude,
    not a fresh (possibly divergent) search."""
    doc = LayoutDocument()
    scene = LayoutScene(doc)
    a, b = _two_straights_in_a_line(doc)
    scene.add_instance_item(a.id)
    scene.add_instance_item(b.id)
    route = doc.add_route(a.id, "o2", b.id, "o1", "strip", goal_length_um=250.0, auto_match=True)
    scene.add_route_item(route.id)
    original_len = _len_um(doc, route)

    path = tmp_path / "p.phidler"
    save_project(doc, str(path))

    doc2 = LayoutDocument()
    scene2 = LayoutScene(doc2)
    load_project(str(path), doc2, scene2)
    reloaded = next(iter(doc2.routes.values()))
    assert reloaded.goal_length_um == 250.0
    assert reloaded.auto_match is True
    assert math.isclose(reloaded.meander_amplitude_um, route.meander_amplitude_um, abs_tol=1e-6)
    assert math.isclose(_len_um(doc2, reloaded), original_len, abs_tol=1e-3)


def test_goal_length_time_units_convert_via_n_eff(qapp):
    from phidler.canvas.view import C0_UM_PER_FS
    from phidler.main_window import MainWindow

    win = MainWindow()
    win.view.set_n_eff(2.0)
    win.route_goal_spin.setValue(1000.0)  # 1000 fs
    win.route_goal_unit_combo.setCurrentText("fs")
    # length = time * c0 / n_eff
    expected_um = 1000.0 * C0_UM_PER_FS / 2.0
    assert math.isclose(win._route_goal_length_um(), expected_um, rel_tol=1e-9)

    win.route_goal_spin.setValue(0.0)  # 0 -> no goal
    assert win._route_goal_length_um() is None
