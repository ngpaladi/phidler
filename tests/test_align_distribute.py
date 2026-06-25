import math

from phidler.main_window import MainWindow
from phidler.model.document import Transform


def _place_three(win) -> tuple[int, int, int]:
    win._place_straight_waveguide()
    a_id = next(iter(win.document.instances))
    win._place_straight_waveguide()
    b_id = [i for i in win.document.instances if i != a_id][0]
    win._place_straight_waveguide()
    c_id = [i for i in win.document.instances if i not in (a_id, b_id)][0]
    return a_id, b_id, c_id


def _set_transform_and_sync(win, inst_id: int, t: Transform) -> None:
    win.document.set_transform(inst_id, t)
    item = win.scene.items_by_inst[inst_id]
    item.apply_transform(t.x, t.y, t.rotation, t.mirror, t.mag)


def _select(win, *inst_ids: int) -> None:
    for inst_id in inst_ids:
        win.scene.items_by_inst[inst_id].setSelected(True)


def _bbox(win, inst_id: int):
    item = win.scene.items_by_inst[inst_id]
    return item.mapRectToScene(item.boundingRect())


def test_align_left_moves_to_the_minimum_left_edge(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=5.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=-3.0, y=0.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._align_selected("left")

    lefts = {inst_id: _bbox(win, inst_id).left() for inst_id in (a, b, c)}
    assert math.isclose(lefts[a], lefts[b], abs_tol=1e-6)
    assert math.isclose(lefts[b], lefts[c], abs_tol=1e-6)
    assert math.isclose(lefts[a], -3.0, abs_tol=1e-6)  # c's original left edge was the minimum


def test_align_top_uses_the_visual_screen_direction_not_qrectf_naming(qapp):
    """The canvas's global Y-flip means larger scene-y renders higher on
    screen — confirmed empirically before writing this. QRectF.top() is
    the *smaller* y (the visual bottom here), so "Align Top" must use
    box.bottom() internally, not box.top(), or it would silently align to
    the visual bottom instead — this test is the one that would catch
    that mistake."""
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=0.0, y=10.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=0.0, y=-5.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._align_selected("top")

    tops = {inst_id: _bbox(win, inst_id).bottom() for inst_id in (a, b, c)}  # bottom() = visual top, see docstring
    assert math.isclose(tops[a], tops[b], abs_tol=1e-6)
    assert math.isclose(tops[b], tops[c], abs_tol=1e-6)
    assert math.isclose(tops[a], 10.25, abs_tol=1e-6)  # b's original top edge (y=10 + half-width 0.25)


def test_align_bottom_uses_the_visual_screen_direction(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=0.0, y=10.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=0.0, y=-5.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._align_selected("bottom")

    bottoms = {inst_id: _bbox(win, inst_id).top() for inst_id in (a, b, c)}  # top() = visual bottom
    assert math.isclose(bottoms[a], bottoms[c], abs_tol=1e-6)
    assert math.isclose(bottoms[a], -5.25, abs_tol=1e-6)  # c's original bottom edge (y=-5 - half-width 0.25)


def test_align_right_moves_to_the_maximum_right_edge(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=20.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=5.0, y=0.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._align_selected("right")

    rights = {inst_id: _bbox(win, inst_id).right() for inst_id in (a, b, c)}
    assert math.isclose(rights[a], rights[b], abs_tol=1e-6)
    assert math.isclose(rights[a], 30.0, abs_tol=1e-6)  # b's original right edge (x=20 + length 10)


def test_align_horizontal_centers_uses_the_mean_center(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=10.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=20.0, y=0.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._align_selected("center_h")

    centers = [_bbox(win, inst_id).center().x() for inst_id in (a, b, c)]
    assert all(math.isclose(c1, centers[0], abs_tol=1e-6) for c1 in centers)
    # mean of (5, 15, 25) [each instance's own center before alignment] is 15
    assert math.isclose(centers[0], 15.0, abs_tol=1e-6)


def test_align_vertical_centers_uses_the_mean_center(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=0.0, y=6.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=0.0, y=-6.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._align_selected("center_v")

    centers = [_bbox(win, inst_id).center().y() for inst_id in (a, b, c)]
    assert all(math.isclose(c1, centers[0], abs_tol=1e-6) for c1 in centers)
    assert math.isclose(centers[0], 0.0, abs_tol=1e-6)


def test_align_with_fewer_than_two_selected_does_nothing(qapp):
    win = MainWindow()
    a, b, _c = _place_three(win)
    _select(win, a)
    before = win.undo_stack.count()

    win._align_selected("left")

    assert win.undo_stack.count() == before


def test_align_already_aligned_instances_pushes_no_undo_command(qapp):
    """An align that wouldn't actually move anything (already aligned)
    must not push a useless undo entry — only non-zero shifts count."""
    win = MainWindow()
    a, b, _c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=0.0, y=5.0, rotation=0.0, mirror=False))
    _select(win, a, b)
    before = win.undo_stack.count()

    win._align_selected("left")  # both already have left=0.0

    assert win.undo_stack.count() == before


def test_align_pushes_a_single_undoable_macro(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=5.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=-3.0, y=0.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)
    before = win.undo_stack.count()

    win._align_selected("left")
    assert win.undo_stack.count() == before + 1

    win.undo_stack.undo()
    assert math.isclose(win.document.get_transform(a).x, 0.0, abs_tol=1e-6)
    assert math.isclose(win.document.get_transform(b).x, 5.0, abs_tol=1e-6)
    assert math.isclose(win.document.get_transform(c).x, -3.0, abs_tol=1e-6)


def test_distribute_horizontally_spaces_centers_evenly_keeping_extremes_fixed(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=1.0, y=0.0, rotation=0.0, mirror=False))  # crowded near a
    _set_transform_and_sync(win, c, Transform(x=30.0, y=0.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._distribute_selected("x")

    centers = {inst_id: _bbox(win, inst_id).center().x() for inst_id in (a, b, c)}
    assert math.isclose(centers[a], 5.0, abs_tol=1e-6)  # extreme: unchanged
    assert math.isclose(centers[c], 35.0, abs_tol=1e-6)  # extreme: unchanged
    assert math.isclose(centers[b], 20.0, abs_tol=1e-6)  # exact midpoint of the two extremes


def test_distribute_vertically_spaces_centers_evenly(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=0.0, y=2.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=0.0, y=40.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)

    win._distribute_selected("y")

    centers = {inst_id: _bbox(win, inst_id).center().y() for inst_id in (a, b, c)}
    assert math.isclose(centers[a], 0.0, abs_tol=1e-6)
    assert math.isclose(centers[c], 40.0, abs_tol=1e-6)
    assert math.isclose(centers[b], 20.0, abs_tol=1e-6)


def test_distribute_with_fewer_than_three_selected_does_nothing(qapp):
    win = MainWindow()
    a, b, _c = _place_three(win)
    _select(win, a, b)
    before = win.undo_stack.count()

    win._distribute_selected("x")

    assert win.undo_stack.count() == before


def test_distribute_pushes_a_single_undoable_macro(qapp):
    win = MainWindow()
    a, b, c = _place_three(win)
    _set_transform_and_sync(win, a, Transform(x=0.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, b, Transform(x=1.0, y=0.0, rotation=0.0, mirror=False))
    _set_transform_and_sync(win, c, Transform(x=30.0, y=0.0, rotation=0.0, mirror=False))
    _select(win, a, b, c)
    before = win.undo_stack.count()

    win._distribute_selected("x")
    assert win.undo_stack.count() == before + 1

    win.undo_stack.undo()
    assert math.isclose(win.document.get_transform(b).x, 1.0, abs_tol=1e-6)


def test_align_and_distribute_actions_are_wired_into_edit_and_context_menus(qapp):
    win = MainWindow()
    assert len(win.align_actions) == 6
    assert len(win.distribute_actions) == 2

    context_menu = win._build_canvas_context_menu()
    all_actions_in_submenus = []
    for action in context_menu.actions():
        if action.menu() is not None:
            all_actions_in_submenus.extend(action.menu().actions())
    for action in win.align_actions + win.distribute_actions:
        assert action in all_actions_in_submenus
