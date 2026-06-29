"""The out-of-process FDTD runner (used for the GPU backend) rebuilds the
document from the saved project and returns a result identical to running
in-process — exercised here on the CPU engine so it needs no GPU."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from phidler.fdtd_sim import FdtdParams, SourceSpec, build_simulation, run_simulation
from phidler.fdtd_subprocess import (
    _params_from_dict,
    _params_to_dict,
    run_in_subprocess,
    write_bundle,
)
from phidler.model.document import LayoutDocument


def test_params_round_trip_through_json_dict():
    params = FdtdParams(
        cell_size_um=0.06,
        use_numba=True,
        sources=(SourceSpec(x_um=-5.0, y_um=1.0, kind="cherenkov", velocity_beta=0.9),),
    )
    restored = _params_from_dict(_params_to_dict(params))
    assert restored == params  # frozen dataclasses compare by value, incl. the source tuple


def test_subprocess_run_matches_in_process(qapp):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 12.0, "width": 0.5})
    doc.add_instance("mmi1x2", {}, x=-18.0, y=0.0)
    params = FdtdParams(
        cell_size_um=0.08,
        use_numba=True,
        sources=(SourceSpec(x_um=-15.0, y_um=0.0, kind="cherenkov"),),
    )

    in_process = run_simulation(build_simulation(doc, params)).fields["field"]["Ez"]
    sim_stub, result_stub, elapsed = run_in_subprocess(doc, params)

    sub = result_stub.fields["field"]["Ez"]
    assert sub.shape == in_process.shape
    assert np.allclose(sub, in_process)  # the child rebuilt the exact same simulation
    assert elapsed > 0
    # the stub exposes just what the display reads
    assert len(sim_stub.grid.coords) == 3
    assert sim_stub.grid.shape[0] == sub.shape[1]


def test_subprocess_applies_the_region(qapp):
    """region_um survives serialization into the child (job JSON -> list -> tuple)
    and actually shrinks the gridded domain there."""
    from phidler.fdtd_sim import estimate_grid_cell_count

    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 20.0, "width": 0.5}, x=0.0, y=0.0)
    doc.add_instance("mmi1x2", {}, x=200.0, y=150.0)  # far away -> huge full grid
    params = FdtdParams(cell_size_um=0.1, use_numba=True, sources=(SourceSpec(x_um=-8.0, y_um=0.0),))
    region = (-15.0, -6.0, 15.0, 6.0)

    sim_stub, _, _ = run_in_subprocess(doc, params, region_um=region)
    roi_cells = sim_stub.grid.shape[0] * sim_stub.grid.shape[1] * sim_stub.grid.shape[2]
    assert roi_cells == estimate_grid_cell_count(doc, params, region_um=region)  # region took effect in child
    assert roi_cells < estimate_grid_cell_count(doc, params) / 10  # far smaller than the full layout


def test_bundle_uses_basenames_not_absolute_paths(qapp, tmp_path):
    """write_bundle must store *basenames* in job.json, so the whole bundle dir
    can be relocated (e.g. scp'd to a remote host) and still resolve."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 8.0, "width": 0.5})
    params = FdtdParams(cell_size_um=0.1, use_numba=True, sources=(SourceSpec(x_um=-5.0, y_um=0.0),))

    job_path = write_bundle(tmp_path, doc, params)
    job = json.loads(job_path.read_text())
    assert job["project"] == "job.phidler"  # not an absolute path
    assert job["out"] == "result.npz"
    assert (tmp_path / "job.phidler").exists()


def test_relocated_bundle_still_runs(qapp, tmp_path):
    """The whole point of basenames: build the bundle in one directory, move it
    to a *different* one, and `python -m phidler.fdtd_subprocess` resolves
    project/out relative to the moved job.json — proving a remote run will work
    after the bundle is copied across machines."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 8.0, "width": 0.5})
    params = FdtdParams(cell_size_um=0.1, use_numba=True, sources=(SourceSpec(x_um=-5.0, y_um=0.0),))

    build_dir = tmp_path / "built_here"
    build_dir.mkdir()
    write_bundle(build_dir, doc, params)

    moved_dir = tmp_path / "ran_there"  # a path the bundle was never built in
    shutil.move(str(build_dir), str(moved_dir))

    proc = subprocess.run(
        [sys.executable, "-m", "phidler.fdtd_subprocess", str(moved_dir / "job.json")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = moved_dir / "result.npz"  # written next to the moved job.json, not the original dir
    assert out.exists()
    with np.load(out) as data:
        assert data["ez"].shape[0] > 0  # a non-empty field movie came back
        assert tuple(data["shape"]).__len__() == 3  # full 3D grid shape recorded


def test_progress_marker_round_trips():
    """The child prints markers that the parent parses back to (step, n)."""
    from phidler.fdtd_subprocess import _PROGRESS_PREFIX, parse_progress_line

    assert parse_progress_line(f"{_PROGRESS_PREFIX} 7 42") == (7, 42)
    assert parse_progress_line("just some log line") is None
    assert parse_progress_line(f"{_PROGRESS_PREFIX} oops") is None  # malformed, not a crash


def test_subprocess_streams_progress_to_callback(qapp):
    """run_in_subprocess forwards the child's streamed progress markers, ending
    at 100% (step == n_steps)."""
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 8.0, "width": 0.5})
    params = FdtdParams(cell_size_um=0.1, use_numba=True, sources=(SourceSpec(x_um=-5.0, y_um=0.0),))

    ticks = []
    run_in_subprocess(doc, params, progress_callback=lambda i, n: ticks.append((i, n)))
    assert ticks  # progress was reported
    assert ticks[0][0] == 0  # started at the first step
    assert ticks[-1][0] == ticks[-1][1]  # finished at 100%


def test_subprocess_reports_the_backend_actually_used(qapp):
    """The child reports the backend it really ran on (not what was requested),
    so a silent CPU fallback in the child is visible rather than just slow."""
    from phidler.fdtd_sim import gpu_available, numba_available

    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 8.0, "width": 0.5})
    src = (SourceSpec(x_um=-5.0, y_um=0.0),)

    if numba_available():
        sim_stub, _, _ = run_in_subprocess(doc, FdtdParams(cell_size_um=0.1, use_numba=True, sources=src))
        assert sim_stub.use_numba is True and sim_stub.use_gpu is False

    if gpu_available():
        sim_stub, _, _ = run_in_subprocess(doc, FdtdParams(cell_size_um=0.1, use_gpu=True, sources=src))
        assert sim_stub.use_gpu is True  # GPU actually engaged in the child, not a quiet fallback
