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

import collections
import dataclasses
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from .fdtd_sim import FdtdParams, SourceSpec

# The child (run as `python -m phidler.fdtd_subprocess`) prints one of these
# lines to stdout every ~1% of the solve, so the parent — local subprocess or
# remote over SSH — can drive a progress bar. A distinctive prefix keeps it from
# being confused with any other output, and the same parser serves both paths.
_PROGRESS_PREFIX = "@@PHIDLER_PROGRESS"

# How many trailing non-progress output lines to keep for an error message if
# the child fails (stderr is merged into stdout in the streaming path).
_ERROR_TAIL_LINES = 40


def emit_progress_marker(step: int, n_steps: int) -> None:
    """Child-side: print a progress marker the parent will parse. Flushed so the
    parent sees ticks promptly rather than in a buffered burst at the end."""
    print(f"{_PROGRESS_PREFIX} {step} {n_steps}", flush=True)


def parse_progress_line(line: str) -> tuple[int, int] | None:
    """Parent-side: ``(step, n_steps)`` if ``line`` is a progress marker, else
    None (so the caller can treat everything else as ordinary output)."""
    if line.startswith(_PROGRESS_PREFIX):
        parts = line.split()
        try:
            return int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return None
    return None


def _params_to_dict(params: FdtdParams) -> dict:
    return dataclasses.asdict(params)


def _params_from_dict(d: dict) -> FdtdParams:
    d = dict(d)
    sources = tuple(SourceSpec(**s) for s in d.pop("sources", ()) or ())
    return FdtdParams(sources=sources, **d)


def write_bundle(tmp: Path, document, params: FdtdParams, region_um=None) -> Path:
    """Write a self-contained, *relocatable* job bundle into directory ``tmp``:
    the saved project (``job.phidler``), the result target (``result.npz``,
    created by the run), and ``job.json`` tying them together. Returns the
    job.json path.

    ``job.json`` stores **basenames**, not absolute paths, so the whole ``tmp``
    directory can be copied elsewhere (e.g. scp'd to a remote host) and still
    resolve — ``_run_job`` joins these names against the job.json's own
    directory. Shared by the local subprocess path (run_in_subprocess) and the
    remote path (fdtd_remote.run_on_remote) so both produce an identical bundle
    and can't drift from the result contract."""
    from .project_io import save_project  # local import keeps this module light to import

    project_path = tmp / "job.phidler"
    out_path = tmp / "result.npz"
    save_project(document, str(project_path))
    job_path = tmp / "job.json"
    job_path.write_text(json.dumps({
        "project": project_path.name,
        "params": _params_to_dict(params),
        "region_um": list(region_um) if region_um is not None else None,
        "out": out_path.name,
    }))
    return job_path


def load_result_npz(path) -> tuple[Any, Any]:
    """Parse a result ``.npz`` (written by _run_job) into the
    ``(sim_stub, result_stub)`` pair the display reads: ``sim_stub.grid.coords``
    / ``.shape`` / ``.use_gpu`` / ``.use_numba`` and
    ``result_stub.fields['field']['Ez']``. Shared by the local and remote paths
    so both hand the UI exactly the same shape."""
    with np.load(path) as data:
        grid = SimpleNamespace(
            coords=[data["x"], data["y"], data["z"]],
            shape=tuple(int(s) for s in data["shape"]),
        )
        # The run reports the backend it *actually* used, not what was
        # requested: cupy may be importable in the parent but fall back to
        # CPU in the child, which would otherwise be an invisible slowdown.
        sim_stub = SimpleNamespace(
            grid=grid,
            use_gpu=bool(data["use_gpu"]),
            use_numba=bool(data["use_numba"]),
        )
        result_stub = SimpleNamespace(fields={"field": {"Ez": data["ez"]}})
    return sim_stub, result_stub


def run_in_subprocess(
    document, params: FdtdParams, region_um=None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[Any, Any, float]:
    """Build and run the simulation in a child process, returning
    ``(sim_stub, result_stub, elapsed)`` shaped exactly like the in-process
    path so the caller is none the wiser: ``sim_stub.grid.coords`` / ``.shape``
    and ``result_stub.fields['field']['Ez']`` are all the display reads.
    ``region_um`` (left, bottom, right, top in µm) restricts the simulated xy
    window, the same as build_simulation. ``progress_callback(step, n_steps)``,
    if given, is called as the child streams progress markers.

    Raises RuntimeError (with the child's error message) if the child fails.
    """
    with tempfile.TemporaryDirectory(prefix="phidler_fdtd_") as tmp_name:
        tmp = Path(tmp_name)
        job_path = write_bundle(tmp, document, params, region_um)
        out_path = tmp / "result.npz"

        t0 = time.time()
        returncode, tail = _stream_child(
            [sys.executable, "-m", "phidler.fdtd_subprocess", str(job_path)],
            progress_callback,
        )
        elapsed = time.time() - t0

        if returncode != 0 or not out_path.exists():
            raise RuntimeError(_error_from_tail(tail, returncode))

        sim_stub, result_stub = load_result_npz(out_path)
        return sim_stub, result_stub, elapsed


def _stream_child(
    cmd: list[str], progress_callback: Callable[[int, int], None] | None
) -> tuple[int, list[str]]:
    """Run ``cmd``, forwarding progress markers to ``progress_callback`` as they
    arrive and keeping the last few non-progress lines for an error message.
    Returns ``(returncode, tail_lines)``. stderr is merged into stdout so the two
    streams can't deadlock filling separate pipes while we read one of them."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    tail: collections.deque[str] = collections.deque(maxlen=_ERROR_TAIL_LINES)
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        prog = parse_progress_line(line)
        if prog is not None:
            if progress_callback is not None:
                progress_callback(prog[0], prog[1])
        elif line:
            tail.append(line)
    return proc.wait(), list(tail)


def _error_from_tail(tail: list[str], returncode: int) -> str:
    return (tail[-1] if tail else "") or f"FDTD subprocess exited with code {returncode}"


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

    def add_annotation_item(self, *a, **k):
        pass

    def clear_annotation_items(self, *a, **k):
        pass


def _run_job(job_path: str) -> None:
    """Child-process entry point: rebuild the document from the saved project,
    run the simulation, and write the (small) result to the output npz."""
    job_dir = Path(job_path).resolve().parent
    job = json.loads(Path(job_path).read_text())
    # project/out are resolved relative to the bundle's own directory, so a
    # bundle that was copied to a different machine (a remote host) still finds
    # its files. Absolute paths in older/local bundles survive this join
    # unchanged (pathlib: dir / "/abs" -> "/abs").
    project_path = job_dir / job["project"]
    out_path = job_dir / job["out"]

    # Activate the same PDK the app uses, without importing the Qt app module.
    from gdsfactory.gpdk import get_generic_pdk

    get_generic_pdk().activate()

    from .fdtd_sim import build_simulation, limit_solver_threads, run_simulation
    from .model.document import LayoutDocument
    from .project_io import load_project

    document = LayoutDocument()
    load_project(str(project_path), document, _NullScene())
    params = _params_from_dict(job["params"])
    region = job.get("region_um")
    region_um = tuple(region) if region is not None else None

    # Cap the numba solve to leave the executing machine some cores, and drop
    # this child below interactive priority. This process is dedicated to the
    # solve, so both are safe here — and it's the entry point the SSH remote
    # host and the nereid server run too, so their desktops don't freeze either
    # (the GPU/CuPy path ignores the numba cap, and renice never hurts it).
    limit_solver_threads(renice=True)

    sim = build_simulation(document, params, region_um=region_um)
    # Stream progress out as stdout markers; the parent (local subprocess or
    # remote over SSH) parses them to drive its progress bar.
    sim.progress_callback = emit_progress_marker
    result = run_simulation(sim)

    coords = sim.grid.coords
    np.savez(
        str(out_path),
        ez=np.asarray(result.fields["field"]["Ez"]),
        x=np.asarray(coords[0]),
        y=np.asarray(coords[1]),
        z=np.asarray(coords[2]),
        shape=np.asarray([int(s) for s in sim.grid.shape]),
        # The backend the Simulation resolved to (use_gpu is AND-ed with cupy
        # availability *in this process*), so the parent can tell the user
        # whether GPU actually engaged or quietly fell back to CPU.
        use_gpu=bool(getattr(sim, "use_gpu", False)),
        use_numba=bool(getattr(sim, "use_numba", False)),
    )


if __name__ == "__main__":
    _run_job(sys.argv[1])
