"""Component files: import, library folders, export (phidler.component_files)."""
import json

import numpy as np
import pytest

from phidler import chip_circuit as cc
from phidler import component_files as cfm
from phidler.model.document import LayoutDocument
from tests.test_chip_circuit import V_PI, fast_lnoi  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not (cc.AVAILABLE and cfm.AVAILABLE),
                                reason="needs photonfdtd.circuit")


def _switch_on_pad():
    cc.register_cells()
    doc = LayoutDocument()
    sw = doc.add_instance("lnoi_mzi_switch", {"arm_length": 500.0})
    pad = doc.add_instance("pad", {}, x=-300.0, y=250.0)
    doc.add_route(pad.id, "e4", sw.id, "sig_w", cross_section="metal_routing")
    return doc, sw, pad


def test_single_instance_round_trip_through_the_palette(qapp, fast_lnoi, tmp_path):
    from phidler.main_window import MainWindow

    doc, sw, _ = _switch_on_pad()
    path = tmp_path / ("switch" + cfm.SUFFIX)
    data = cfm.export_selection(doc, [sw.id], str(path), name="my switch",
                                sparams_wavelengths=np.linspace(1.5, 1.6, 5))
    assert {"physics", "recipe", "sparams"} <= set(data["model"])
    assert data["generator"]["function"] == "mzi_switch"

    win = MainWindow()
    win._apply_component_file(str(path), trust=False)
    specs = win.catalog.get(cfm.CATEGORY, [])
    assert [s.name for s in specs] == ["pcomp_my_switch"]
    # The generator lives in photonfdtd, so its parameters are editable.
    assert "arm_length" in specs[0].signature.parameters
    placed = win.document.add_instance("pcomp_my_switch", {})
    assert {p.name for p in placed.cell.ports} >= {"o1", "o2", "o3", "o4", "sig_w"}
    regen = win.document.add_instance("pcomp_my_switch", {"arm_length": 800.0}, y=500.0)
    assert regen.cell.settings.arm_length == 800
    # Re-importing the same file replaces its palette entry.
    win._apply_component_file(str(path), trust=False)
    assert len(win.catalog[cfm.CATEGORY]) == 1


def test_selection_exports_as_a_circuit_and_places_elsewhere(qapp, fast_lnoi, tmp_path):
    doc, sw, pad = _switch_on_pad()
    path = tmp_path / ("sub" + cfm.SUFFIX)
    data = cfm.export_selection(doc, [sw.id, pad.id], str(path), name="switch on pad")
    assert data["model"]["physics"]["kind"] == "circuit"
    optical = [p["name"] for p in data["ports"] if p["type"] == "optical"]
    assert optical == ["o1", "o2", "o3", "o4"]
    pins = data["conductors"]
    pad_pin = next(n for n, ports in pins.items() if any(p.startswith("pad") for p in ports))

    loaded = cfm.load_component_file(str(path))
    doc2 = LayoutDocument()
    inst = doc2.add_instance(loaded.spec.name, {})
    circ = cc.build_circuit(doc2, cc.CircuitConfig())
    name = next(iter(circ.chip.models))
    ext_in = f"{name},o1"
    port_of_pin = pins[pad_pin][0]
    for v in (0.0, V_PI):
        st = circ.chip.solve(1.55, drives={f"{name},{port_of_pin}": v}, inputs={ext_in: 1.0})
        powers = {k.split(",")[1]: st.power[k][0] for k in st.power}
        best = max(powers, key=powers.get)
        assert powers[best] > 0.99
        if v == 0.0:
            out0 = best
    assert best != out0          # the pad switches the light


def test_library_folder_and_settings(qapp, fast_lnoi, tmp_path):
    from PySide6.QtCore import QSettings

    from phidler.component_libraries import LibrariesDialog, library_dirs, set_library_dirs

    doc, sw, _ = _switch_on_pad()
    lib = tmp_path / "mylib"
    lib.mkdir()
    cfm.export_selection(doc, [sw.id], str(lib / "a"), name="lib switch")
    (lib / ("broken" + cfm.SUFFIX)).write_text("{}")
    loaded, failed = cfm.scan_library(str(lib))
    assert [x.spec.name for x in loaded] == ["pcomp_lib_switch"]
    assert loaded[0].spec.category == "library:mylib" and len(failed) == 1
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    set_library_dirs([str(lib)], settings)
    assert library_dirs(settings) == [str(lib)]
    dlg = LibrariesDialog(settings=settings)
    assert dlg.folders() == [str(lib)]


def test_project_reload_keeps_component_file_instances(qapp, fast_lnoi, tmp_path):
    from phidler.main_window import MainWindow
    from phidler.project_io import save_project

    doc, sw, _ = _switch_on_pad()
    path = tmp_path / ("p" + cfm.SUFFIX)
    cfm.export_selection(doc, [sw.id], str(path), name="proj switch")
    win = MainWindow()
    win._apply_component_file(str(path), trust=False)
    win.document.add_instance("pcomp_proj_switch", {})
    proj = tmp_path / "x.phidler"
    save_project(win.document, str(proj))
    assert str(path) in json.loads(proj.read_text())["custom_component_paths"]
    win2 = MainWindow()
    from phidler.project_io import load_project
    load_project(str(proj), win2.document, win2.scene)
    assert [i.component_spec for i in win2.document.instances.values()] == ["pcomp_proj_switch"]


def test_generator_source_asks_for_trust(qapp, tmp_path, monkeypatch):
    import gdsfactory as gf
    from PySide6.QtWidgets import QMessageBox

    from photonfdtd.circuit import library as pclib
    from phidler.main_window import MainWindow

    c = gf.Component("blk")
    c.add_polygon([(0, 0), (2, 0), (2, 2), (0, 2)], layer=(1, 0))
    src = ("import gdsfactory as gf\n"
           "def block(size=2.0):\n"
           "    c = gf.Component()\n"
           "    c.add_polygon([(0, 0), (size, 0), (size, size), (0, size)], layer=(1, 0))\n"
           "    return c\n")
    pclib.export_component(c, tmp_path / "blk", name="block", model=None,
                           generator={"function": "block", "kwargs": {"size": 2.0}},
                           generator_source=src)
    path = str(tmp_path / ("blk" + cfm.SUFFIX))
    assert cfm.needs_trust(path)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.No)
    win = MainWindow()
    win._apply_component_file(path)
    assert asked
    spec = win.catalog_by_name["pcomp_block"]
    assert not spec.signature.parameters           # untrusted: fixed geometry only


def test_route_to_an_unselected_part_ends_in_a_pin(qapp, fast_lnoi):
    doc, sw, pad = _switch_on_pad()
    c = cfm.selection_component(doc, [sw.id])
    ports = {p.name: p for p in c.ports}
    # The route from sig_w to the unselected pad comes along, and its end at
    # the pad is the pin.
    assert f"lnoi_mzi_switch{sw.id}_sig_w" not in ports
    pin = ports[f"to_pad{pad.id}_e4"]
    assert tuple(round(x, 3) for x in pin.center) == tuple(
        round(x, 3) for x in pad.ref.ports["e4"].center)
    assert {"o1", "o2", "o3", "o4"} <= set(ports)
    both = cfm.selection_component(doc, [sw.id, pad.id])
    assert not any(n.startswith("to_") for n in (p.name for p in both.ports))


def test_reexport_of_an_imported_part_keeps_its_file(qapp, fast_lnoi, tmp_path):
    doc, sw, _ = _switch_on_pad()
    first = cfm.export_selection(doc, [sw.id], str(tmp_path / "a"), name="orig",
                                 sparams_wavelengths=np.linspace(1.5, 1.6, 3))
    loaded = cfm.load_component_file(str(tmp_path / ("a" + cfm.SUFFIX)))
    doc2 = LayoutDocument()
    inst = doc2.add_instance(loaded.spec.name, {})
    again = cfm.export_selection(doc2, [inst.id], str(tmp_path / "b"), name="copy")
    assert again["model"] == first["model"] and again["generator"] == first["generator"]


def test_trust_is_remembered_per_file_content(qapp, tmp_path, monkeypatch):
    import gdsfactory as gf
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox

    from photonfdtd.circuit import library as pclib
    from phidler.main_window import MainWindow

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    monkeypatch.setattr(cfm, "_settings", lambda s=None: settings)
    c = gf.Component("blk2")
    c.add_polygon([(0, 0), (2, 0), (2, 2), (0, 2)], layer=(1, 0))
    src = ("import gdsfactory as gf\n"
           "def block2(size=2.0):\n"
           "    c = gf.Component()\n"
           "    c.add_polygon([(0, 0), (size, 0), (size, size), (0, size)], layer=(1, 0))\n"
           "    return c\n")
    pclib.export_component(c, tmp_path / "blk2", name="block2", model=None,
                           generator={"function": "block2", "kwargs": {"size": 2.0}},
                           generator_source=src)
    path = str(tmp_path / ("blk2" + cfm.SUFFIX))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.Yes)
    win = MainWindow()
    win._apply_component_file(path)
    assert asked == [1] and cfm.is_trusted(cfm.file_key(path), settings)
    # A reload (as on project open) is trusted without asking again.
    loaded = cfm.load_component_file(path)
    assert "size" in loaded.spec.signature.parameters
    win._apply_component_file(path)
    assert asked == [1]
    # Untrusted, saved parameters fall back to the fixed geometry.
    settings.remove(cfm.TRUST_KEY)
    pclib._REGISTRY.clear()
    plain = cfm.load_component_file(path)
    with pytest.warns(UserWarning, match="not trusted"):
        cell = plain.spec.factory(size=3.0)
    assert cell.dbbox().width() == pytest.approx(2.0)


def test_fan_out_route_to_a_selected_part_is_kept(qapp, fast_lnoi):
    doc, sw, pad = _switch_on_pad()
    other = doc.add_instance("pad", {}, x=-300.0, y=700.0)
    doc.add_route(pad.id, "e4", other.id, "e4", cross_section="metal_routing")
    c = cfm.selection_component(doc, [sw.id, pad.id])
    names = {p.name for p in c.ports}
    # The route to the selected switch stays (sig_w is not a pin), and the
    # branch to the unselected pad ends in a pin.
    assert f"lnoi_mzi_switch{sw.id}_sig_w" not in names
    assert f"to_pad{other.id}_e4" in names


def test_unmodelled_selection_exports_layout_only(qapp, tmp_path):
    doc = LayoutDocument()
    ring = doc.add_instance("ring_single", {})
    with pytest.warns(UserWarning, match="without a circuit model"):
        data = cfm.export_selection(doc, [ring.id], str(tmp_path / "r"), name="ring")
    assert "physics" not in data["model"] and data["generator"]["function"] == "ring_single"
    loaded = cfm.load_component_file(str(tmp_path / ("r" + cfm.SUFFIX)))
    assert "gap" in loaded.spec.signature.parameters   # a gdsfactory cell: editable untrusted


def test_export_dialog_starts_from_the_imported_file(qapp, fast_lnoi, tmp_path):
    from phidler.component_libraries import ExportComponentDialog

    doc, sw, _ = _switch_on_pad()
    cfm.export_selection(doc, [sw.id], str(tmp_path / "a"), name="nice name",
                         description="from the lab", author="someone")
    loaded = cfm.load_component_file(str(tmp_path / ("a" + cfm.SUFFIX)))
    doc2 = LayoutDocument()
    inst = doc2.add_instance(loaded.spec.name, {})
    dlg = ExportComponentDialog(None, doc2, [inst.id])
    assert dlg.name_edit.text() == "nice name" and dlg.desc_edit.text() == "from the lab"
    assert dlg.author_edit.text() == "someone"
