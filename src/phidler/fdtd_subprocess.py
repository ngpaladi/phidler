"""Run an FDTD simulation in a separate process.

This exists for the GPU (CuPy) backend. CuPy's CUDA context cannot be torn
down inside a Qt worker thread without crashing the process, and running it on
the main thread freezes the UI for the whole run. A child process sidesteps
both: it gets its own CUDA context (clean teardown when the process exits), and
the parent's worker thread merely waits on it, so the UI stays responsive.

Shipping the result back is cheap because the recorded movie is just the single
mid-core plane of one component (see build_simulation) — a few MB through a temp
file, not the hundreds of MB a full 3D movie would be.

Run as a module in the child: ``python -m phidler.fdtd_subprocess <job.json>``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from .fdtd_sim import FdtdParams, SourceSpec


def _params_to_dict(params: FdtdParams) -> dict:
    return dataclasses.asdict(params)


def _params_from_dict(d: dict) -> FdtdParams:
    d = dict(d)
    sources = tuple(SourceSpec(**s) for s in d.pop("sources", ()) or ())
    return FdtdParams(sources=sources, **d)


def run_in_subprocess(document, params: FdtdParams) -> tuple[Any, Any, float]:
    """Build and run the simulation in a child process, returning
    ``(sim_stub, result_stub, elapsed)`` shaped exactly like the in-process
    path so the caller is none the wiser: ``sim_stub.grid.coords`` / ``.shape``
    and ``result_stub.fields['field']['Ez']`` are all the display reads.

    Raises RuntimeError (with the child's error message) if the child fails.
    """
    from .project_io import save_project  # local import keeps this module light to import

    with tempfile.TemporaryDirectory(prefix="phidler_fdtd_") as tmp_name:
        tmp = Path(tmp_name)
        project_path = tmp / "job.phidler"
        out_path = tmp / "result.npz"
        save_project(document, str(project_path))
        job_path = tmp / "job.json"
        job_path.write_text(json.dumps({
            "project": str(project_path),
            "params": _params_to_dict(params),
            "out": str(out_path),
        }))

        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, "-m", "phidler.fdtd_subprocess", str(job_path)],
            capture_output=True,
            text=True,
        )
        elapsed = time.time() - t0

        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError(_child_error(proc))

        with np.load(out_path) as data:
            grid = SimpleNamespace(
                coords=[data["x"], data["y"], data["z"]],
                shape=tuple(int(s) for s in data["shape"]),
            )
            sim_stub = SimpleNamespace(grid=grid)
            result_stub = SimpleNamespace(fields={"field": {"Ez": data["ez"]}})
        return sim_stub, result_stub, elapsed


def _child_error(proc: subprocess.CompletedProcess) -> str:
    err = (proc.stderr or "").strip()
    last = err.splitlines()[-1] if err else ""
    return last or f"FDTD subprocess exited with code {proc.returncode}"


class _NullScene:
    """load_project drives a scene to mirror geometry into the GUI; the child
    only needs the rebuilt document, so every scene call is a no-op."""

    def add_instance_item(self, *a, **k):
        pass

    def remove_instance_item(self, *a, **k):
        pass

    def add_route_item(self, *a, **k):
        pass

    def remove_route_item(self, *a, **k):
        pass

    def clear_reference_item(self, *a, **k):
        pass

    def show_reference(self, *a, **k):
        pass


def _run_job(job_path: str) -> None:
    """Child-process entry point: rebuild the document from the saved project,
    run the simulation, and write the (small) result to the output npz."""
    job = json.loads(Path(job_path).read_text())

    # Activate the same PDK the app uses, without importing the Qt app module.
    from gdsfactory.gpdk import get_generic_pdk

    get_generic_pdk().activate()

    from .fdtd_sim import build_simulation, run_simulation
    from .model.document import LayoutDocument
    from .project_io import load_project

    document = LayoutDocument()
    load_project(job["project"], document, _NullScene())
    params = _params_from_dict(job["params"])

    sim = build_simulation(document, params)
    result = run_simulation(sim)

    coords = sim.grid.coords
    np.savez(
        job["out"],
        ez=np.asarray(result.fields["field"]["Ez"]),
        x=np.asarray(coords[0]),
        y=np.asarray(coords[1]),
        z=np.asarray(coords[2]),
        shape=np.asarray([int(s) for s in sim.grid.shape]),
    )


if __name__ == "__main__":
    _run_job(sys.argv[1])
