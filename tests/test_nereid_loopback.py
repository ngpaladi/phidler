"""End-to-end test of the nereid (gRPC) FDTD transport against a *real*
in-process gRPC server — no mocks (unlike test_fdtd_nereid.py, which fakes the
stub). A minimal servicer implements the Nereid contract, stages the streamed
bundle, runs the actual backend (nereid-backend/phidler-fdtd/main.py -> a real
photonfdtd solve), and streams the result back, exactly as nereid-server's Python
branch is specified to. Needs no external host: the server is in-process and the
backend runs as a subprocess of the test's own interpreter.

Skipped unless grpcio (the nereid extra) and photonfdtd (the solver) are both
importable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from concurrent import futures
from pathlib import Path

import pytest

grpc = pytest.importorskip("grpc", reason="nereid extra (grpcio) not installed")
pytest.importorskip("photonfdtd", reason="photonfdtd (the solver) not installed")

from phidler._nereid import pb2, pb2_grpc  # noqa: E402

_BACKEND_MAIN = Path(__file__).resolve().parents[1] / "nereid-backend" / "phidler-fdtd" / "main.py"


class _NereidLoopback(pb2_grpc.NereidServicer):
    """The server-side contract from nereid-backend/README.md, in-process: stage
    the streamed tensors as files, run the backend with NEREID_WORK_DIR /
    NEREID_OUTPUT_FILE, stream its stdout back, then return the output file."""

    def HealthCheck(self, request, context):
        return pb2.HealthCheckResponse(status="ok")

    def ViewModels(self, request, context):
        return pb2.ViewModelsResponse(model_names=["phidler-fdtd"])

    def Checkpoint(self, request_iterator, context):
        work = Path(tempfile.mkdtemp(prefix="nereid_srv_"))
        out_file = "result.npz"
        staged: dict[str, bytearray] = {}
        for req in request_iterator:
            if req.HasField("meta"):
                out_file = req.meta.output_file or out_file
            elif req.HasField("chunk"):
                c = req.chunk
                staged.setdefault(c.tensor_name, bytearray()).extend(c.data)
        for name, data in staged.items():
            (work / name).write_bytes(bytes(data))

        env = dict(os.environ, NEREID_WORK_DIR=str(work), NEREID_OUTPUT_FILE=out_file)
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_BACKEND_MAIN)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:  # live stdout -> chunks (progress markers included)
            yield pb2.CheckpointResponse(chunk=line.rstrip("\n"))
        rc = proc.wait()

        out_path = work / out_file
        if rc == 0 and out_path.is_file():
            data = out_path.read_bytes()
            step = 64 * 1024
            for i in range(0, len(data), step):
                yield pb2.CheckpointResponse(
                    output_chunk=pb2.TensorChunk(
                        tensor_name=out_file, data=data[i : i + step],
                        end_of_tensor=(i + step >= len(data)),
                    )
                )
        yield pb2.CheckpointResponse(done=True, exit_code=rc)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def nereid_server():
    opts = [
        ("grpc.max_send_message_length", 64 * 1024 * 1024),
        ("grpc.max_receive_message_length", 64 * 1024 * 1024),
    ]
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4), options=opts)
    pb2_grpc.add_NereidServicer_to_server(_NereidLoopback(), server)
    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    try:
        yield port
    finally:
        server.stop(0)


def _cfg(port):
    from phidler.fdtd_nereid import NereidConfig

    return NereidConfig(host="127.0.0.1", port=port, model="phidler-fdtd")


def test_check_nereid_against_a_real_server(qapp, nereid_server):
    from phidler.fdtd_nereid import check_nereid

    ok, msg = check_nereid(_cfg(nereid_server))
    assert ok, msg
    assert "phidler-fdtd" in msg


def test_check_nereid_reports_a_missing_model(qapp, nereid_server):
    from phidler.fdtd_nereid import NereidConfig, check_nereid

    ok, msg = check_nereid(NereidConfig(host="127.0.0.1", port=nereid_server, model="not-a-model"))
    assert not ok
    assert "not" in msg.lower()  # names the missing model / available list


def test_run_on_nereid_round_trips_a_real_solve(qapp, nereid_server):
    from gdsfactory.gpdk import get_generic_pdk

    get_generic_pdk().activate()
    from phidler.fdtd_nereid import run_on_nereid
    from phidler.fdtd_sim import FdtdParams, SourceSpec
    from phidler.model.document import LayoutDocument

    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 6.0, "width": 0.5})
    params = FdtdParams(cell_size_um=0.1, use_numba=False, sources=(SourceSpec(x_um=-4.0, y_um=0.0),))

    ticks: list[tuple[int, int]] = []
    sim, result, elapsed = run_on_nereid(
        doc, params, None, _cfg(nereid_server), progress_callback=lambda i, n: ticks.append((i, n))
    )

    ez = result.fields["field"]["Ez"]
    assert ez.shape[0] > 0  # a real field movie came back over gRPC
    assert len(sim.grid.shape) == 3  # full 3D grid metadata parsed from the npz
    assert elapsed >= 0
    assert ticks and ticks[-1][0] == ticks[-1][1]  # progress streamed and reached 100%
