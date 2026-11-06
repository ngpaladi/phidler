"""Training a design box on the layout (phidler.chip_train + the Train tab)."""
import numpy as np
import pytest

from phidler import chip_train as ct
from phidler.model.document import LayoutDocument


def _demo(n=2, **kw):
    doc = LayoutDocument()
    box, ins, outs = ct.demo_priority_encoder_layout(doc, n, **kw)
    return doc, box, ins, outs


# -- tasks ------------------------------------------------------------------ #


def test_priority_encoder_labels_the_highest_lit_line():
    t = ct.priority_encoder_task(3)
    assert t.kind == "classify" and t.X.shape == (7, 3)
    for x, y in zip(t.X.real.astype(int), t.Y):
        assert y == max(i for i, b in enumerate(x) if b)
    th = ct.priority_encoder_task(4, thermometer=True)
    assert th.X.real.astype(int).tolist() == [[1, 0, 0, 0], [1, 1, 0, 0], [1, 1, 1, 0], [1, 1, 1, 1]]
    assert th.Y.tolist() == [0, 1, 2, 3]


def test_regression_targets_are_normalized_to_fractions():
    t = ct.Task("regress", X=[[1, 0], [0, 1]], Y=[[2, 2], [0, 5]])
    assert np.allclose(t.Y, [[0.5, 0.5], [0, 1]])
    with pytest.raises(ValueError):
        ct.Task("regress", X=[[1, 0]], Y=[[0, 0]])


def test_load_task_file_reads_csv_and_npz(tmp_path):
    csv_path = tmp_path / "d.csv"
    csv_path.write_text("in0,in1,label\n1,0,0\n0.5+0.5j,-1j,1\n")
    t = ct.load_task_file(str(csv_path), "classify")
    assert t.X[1].tolist() == [0.5 + 0.5j, -1j] and t.Y.tolist() == [0, 1]
    reg = tmp_path / "r.csv"
    reg.write_text("in0,in1,out0,out1\n1,0,3,1\n")
    assert np.allclose(ct.load_task_file(str(reg), "regress").Y, [[0.75, 0.25]])
    npz = tmp_path / "m.npz"
    np.savez(npz, T=np.eye(2))
    assert ct.load_task_file(str(npz), "transfer").target_T.shape == (2, 2)


def test_fidelity_ignores_scale_and_global_phase():
    t = np.array([[1, 1j], [1j, 1]])
    assert ct.fidelity(3e-14 * np.exp(0.7j) * t, t) == pytest.approx(1.0)
    assert ct.fidelity(np.eye(2), np.array([[0, 1], [1, 0]])) == pytest.approx(0.0)


def test_matrix_text_parses_complex_rows():
    from phidler.panels.train_tab import parse_matrix

    m = parse_matrix("1 1j\n1j, 1\n")
    assert m.tolist() == [[1, 1j], [1j, 1]]
    with pytest.raises(ValueError):
        parse_matrix("1 2\n3")


# -- ports and geometry ----------------------------------------------------- #


def test_open_ports_skip_routed_ports_and_follow_rotation(qapp):
    doc = LayoutDocument()
    a = doc.add_instance("straight", {"length": 2.0}, x=0.0, y=0.0)
    b = doc.add_instance("straight", {"length": 2.0}, x=10.0, y=0.0, rotation=90.0)
    names = {p.name: p for p in ct.open_ports(doc)}
    assert {n.split(":")[1] for n in names} == {"o1", "o2"} and len(names) == 4
    rot = names[f"straight#{b.id}:o2"]
    assert rot.orientation_deg == pytest.approx(90.0) and rot.y_um == pytest.approx(2.0)
    doc.add_route(a.id, "o2", b.id, "o1")
    left = {p.name for p in ct.open_ports(doc)}
    assert f"straight#{a.id}:o2" not in left and f"straight#{b.id}:o1" not in left


def test_demo_layout_puts_inputs_left_and_outputs_right(qapp):
    _doc, box, ins, outs = _demo(4)
    assert [p.orientation_deg for p in ins] == [180.0] * 4
    assert [p.orientation_deg for p in outs] == [0.0] * 4
    assert [p.y_um for p in ins] == sorted(p.y_um for p in ins)  # line 0 at the bottom
    left, bottom, right, top = box
    assert all(p.x_um < left for p in ins) and all(p.x_um > right for p in outs)


def test_model_grid_frame_matches_the_layout(qapp):
    """The design box, sources and readout planes sit where they are on the
    layout, in the grid frame that from_gdsfactory centres on the region."""
    doc, box, ins, outs = _demo(2, center_um=(30.0, -7.0))
    m = ct.ChipModel(doc, ct.TrainConfig(box_um=box, inputs=ins, outputs=outs, cell_size_um=0.1))
    left, bottom, right, top = box
    assert m.box_xs_um.min() >= left - 0.1 and m.box_xs_um.max() <= right + 0.1
    assert m.box_ys_um.min() >= bottom - 0.1 and m.box_ys_um.max() <= top + 0.1
    ox, oy = m.origin_um
    for sim, p in zip(m.sims, ins):
        x, y, _ = sim.sources[0].position
        assert x * 1e6 + ox == pytest.approx(p.x_um + ct._PORT_INSET_UM)
        assert y * 1e6 + oy == pytest.approx(p.y_um)
    # The input stubs are core at the source; the box starts as cladding.
    eps = np.asarray(m.sims[0].eps_r)[:, :, 0]
    ix = int(np.argmin(abs(m.xs_um - (ins[0].x_um + 0.5))))
    iy = int(np.argmin(abs(m.ys_um - ins[0].y_um)))
    assert eps[ix, iy] == pytest.approx(m.reduction.n_core ** 2, rel=1e-3)


def test_model_rejects_a_port_used_twice_and_an_empty_box(qapp):
    doc, box, ins, outs = _demo(2)
    with pytest.raises(ValueError, match="twice"):
        ct.ChipModel(doc, ct.TrainConfig(box_um=box, inputs=ins, outputs=ins))
    with pytest.raises(ValueError, match="no area"):
        ct.ChipModel(doc, ct.TrainConfig(box_um=(0, 0, 0, 1), inputs=ins, outputs=outs))


# -- training --------------------------------------------------------------- #


@pytest.fixture(scope="module")
def small_model(qapp):
    doc, box, ins, outs = _demo(2, gap_um=2.0, pitch_um=1.2, stub_um=1.5)
    cfg = ct.TrainConfig(box_um=box, inputs=ins, outputs=outs, cell_size_um=0.1)
    return doc, ct.ChipModel(doc, cfg)


def test_training_a_two_line_priority_encoder_reaches_full_accuracy(small_model):
    _doc, m = small_model
    task = ct.priority_encoder_task(2)
    seen = []
    r = ct.train_chip(m, task, ct.TrainOptions(steps=25, learning_rate=0.08),
                      callback=lambda rec, rho: seen.append((rec, rho.shape)))
    assert len(seen) == 25 and seen[0][1] == m.shape
    assert all(np.isfinite(rec["value"]) for rec, _ in seen)  # no float32 overflow
    assert r.metrics["accuracy"] == 1.0
    assert r.history[-1]["value"] < r.history[0]["value"]


def test_callback_returning_true_stops_training(small_model):
    _doc, m = small_model
    r = ct.train_chip(m, ct.priority_encoder_task(2), ct.TrainOptions(steps=50),
                      callback=lambda rec, rho: rec["it"] >= 2)
    assert r.stopped and len(r.history) == 3


def test_transfer_and_regression_tasks_train(small_model):
    _doc, m = small_model
    r = ct.train_chip(m, ct.Task("transfer", target_T=[[1, 1j], [1j, 1]]), ct.TrainOptions(steps=4))
    assert 0.0 <= r.metrics["fidelity"] <= 1.0 and np.isfinite(r.history[-1]["value"])
    reg = ct.Task("regress", X=[[1, 0], [0, 1]], Y=[[0.5, 0.5], [0.5, 0.5]])
    r = ct.train_chip(m, reg, ct.TrainOptions(steps=4))
    assert np.isfinite(r.metrics["rms_error"])


def test_task_and_port_count_mismatch_is_reported(small_model):
    _doc, m = small_model
    with pytest.raises(ValueError, match="3 inputs"):
        ct.train_chip(m, ct.priority_encoder_task(3), ct.TrainOptions(steps=1))


# -- placing the result ----------------------------------------------------- #


def test_trained_design_places_saves_and_reloads(qapp, small_model, tmp_path):
    from phidler.project_io import load_project, save_project

    doc, m = small_model
    left, bottom, right, top = m.config.box_um
    rho = np.zeros(m.shape)
    nx, ny = m.shape
    rho[nx // 4: 3 * nx // 4, ny // 4: 3 * ny // 4] = 1.0  # a block of core
    rho[nx // 2 - 2: nx // 2 + 2, ny // 2 - 2: ny // 2 + 2] = 0.0  # with a hole
    inst = ct.place_trained_design(doc, m, rho)
    area = sum(p.area() for p in inst.cell.get_polygons()[inst.cell.kcl.layer(1, 0)]) * inst.cell.kcl.dbu ** 2
    cell_area = (right - left) * (top - bottom) / (nx * ny)
    expected = rho.sum() * cell_area
    assert area == pytest.approx(expected, rel=0.15)
    bb = inst.ref.dbbox()
    assert left - 0.01 <= bb.left and bb.right <= right + 0.01

    class _Scene:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    path = tmp_path / "trained.phidler"
    save_project(doc, str(path))
    fresh = LayoutDocument()
    load_project(str(path), fresh, _Scene())
    again = [i for i in fresh.instances.values() if i.component_spec == ct.TRAINED_CELL]
    assert len(again) == 1
    assert again[0].kwargs["polygons"] == inst.kwargs["polygons"]


# -- the Train tab ---------------------------------------------------------- #


def _pump_until(predicate, timeout_s=240.0):
    import time

    from PySide6.QtCore import QCoreApplication

    end = time.time() + timeout_s
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.02)
        if predicate():
            return True
    return False


def test_train_tab_demo_trains_and_places_a_design(qapp):
    from phidler.main_window import MainWindow
    from phidler.panels.fdtd_window import FdtdWindow

    win = MainWindow()
    fw = FdtdWindow(win.document, win.view)
    tab = fw.train_tab
    tab.pe_lines_spin.setValue(2)
    tab._on_insert_demo()
    assert len(win.document.instances) == 4
    ins, outs = tab.selected_ports()
    assert len(ins) == 2 and len(outs) == 2
    assert tab._box_item is not None and tab._box_item.isVisible()
    # Undo removes the whole demo in one step.
    win.undo_stack.undo()
    assert len(win.document.instances) == 0
    win.undo_stack.redo()
    assert len(win.document.instances) == 4

    tab.cell_spin.setValue(0.1)
    tab.steps_spin.setValue(20)
    tab.lr_spin.setValue(0.08)
    tab._on_train()
    assert _pump_until(lambda: tab.place_button.isEnabled() or tab.train_button.isEnabled())
    assert tab._result is not None, tab.status_label.text()
    assert tab._result.metrics["accuracy"] == 1.0
    assert len(tab.plot.loss) == 20
    # Paint the live widgets (an attribute shadowing QWidget.metric() once broke painting).
    assert not tab.plot.grab().isNull() and not fw.grab().isNull()
    assert tab.results_table.rowCount() == 3  # the 3 non-empty 2-line patterns
    tab._on_place()
    trained = [i for i in win.document.instances.values() if i.component_spec == ct.TRAINED_CELL]
    assert len(trained) == 1
    win.undo_stack.undo()
    assert not [i for i in win.document.instances.values() if i.component_spec == ct.TRAINED_CELL]
    fw.close()
    assert tab._box_item is None


def test_train_tab_rejects_a_task_that_does_not_match_the_ports(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from phidler.main_window import MainWindow
    from phidler.panels.fdtd_window import FdtdWindow

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    win = MainWindow()
    fw = FdtdWindow(win.document, win.view)
    tab = fw.train_tab
    tab.pe_lines_spin.setValue(2)
    tab._on_insert_demo()
    tab.pe_lines_spin.setValue(3)
    tab._on_train()
    assert warned and "3 inputs" in warned[0]
    assert not tab.is_training()
    fw.close()
