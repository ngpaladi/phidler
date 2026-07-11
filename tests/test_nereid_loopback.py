"""End-to-end test of the nereid (gRPC) FDTD transport against a gRPC server
that faithfully replays nereid-server's real Checkpoint→Python contract — no
mocks (test_fdtd_nereid.py fakes the stub). The servicer mirrors what
src/python_backend.rs actually does: it concatenates the input tensor chunks and
pipes them to the backend's stdin (NEREID_INPUT_SHAPE set), runs the real backend
(nereid-backend/phidler-fdtd/main.py -> a real photonfdtd solve), streams its
stdout back, then reads the framed output tensor it writes to NEREID_OUTPUT_PATH,
enforcing the server's guards (float32 header, byte length a multiple of 4),
strips the header, and streams the body back.

This is what makes phidler's opaque job bundle / .npz round-trip through a path
built for a single numeric float32 tensor. Needs no external host; skipped unless
grpcio (the nereid extra) and photonfdtd (the solver) are importable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from concurrent import futures
from pathlib import Path

import pytest

grpc = pytest.importorskip("grpc", reason="nereid extra (grpcio) not installed")
pytest.importorskip("photonfdtd", reason="photonfdtd (the solver) not installed")

from phidler._nereid import pb2, pb2_grpc  # noqa: E402

_MODEL_DIR = Path(__file__).resolve().parents[1] / "nereid-backend" / "phidler-fdtd"
_MAIN_PY = _MODEL_DIR / "main.py"


class _NereidLoopback(pb2_grpc.NereidServicer):
    """Replays nereid-server's Checkpoint→Python behaviour in-process."""

    def HealthCheck(self, request, context):
        return pb2.HealthCheckResponse(status="ok")

    def ViewModels(self, request, context):
        return pb2.ViewModelsResponse(model_names=["phidler-fdtd"])

    def Checkpoint(self, request_iterator, context):
        # 1) Concatenate the input tensor chunks (the server pipes these to stdin).
        input_bytes = bytearray()
        shape: list[int] = []
        for req in request_iterator:
            if req.HasField("chunk"):
                if not shape:
                    shape = list(req.chunk.shape)
                input_bytes += req.chunk.data

        out_path = Path(tempfile.mktemp(prefix="nereid_out_", suffix=".bin"))
        env = dict(
            os.environ,
            NEREID_INPUT_SHAPE=",".join(str(d) for d in (shape or [len(input_bytes)])),
            NEREID_INPUT_DTYPE="float32",
            NEREID_OUTPUT_PATH=str(out_path),
            NEREID_OUTPUT_DTYPE="float32",
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"], cwd=str(_MODEL_DIR), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

        # 2) Feed stdin on a thread (avoids a pipe deadlock on a large bundle).
        def _feed():
            try:
                proc.stdin.write(bytes(input_bytes))
                proc.stdin.close()
            except Exception:
                pass

        threading.Thread(target=_feed, daemon=True).start()

        # 3) Stream stdout back line by line (progress markers reach the client).
        for raw in proc.stdout:
            yield pb2.CheckpointResponse(chunk=raw.decode("utf-8", "replace").rstrip("\n"))
        rc = proc.wait()

        # 4) On success, read the framed output tensor, enforce the server's
        #    guards (float32, multiple-of-4), strip the header, stream the body.
        if rc == 0 and out_path.is_file():
            framed = out_path.read_bytes()
            newline = framed.index(b"\n")
            header = framed[:newline].decode("utf-8")
            body = framed[newline + 1 :]
            dtype = header.split()[0]
            assert dtype == "float32", f"server only accepts float32, got {dtype}"
            assert len(body) % 4 == 0, "float32 tensor byte length must be a multiple of 4"
            step = 64 * 1024
            for i in range(0, len(body), step):
                yield pb2.CheckpointResponse(
                    output_chunk=pb2.TensorChunk(
                        tensor_name="output", data=body[i : i + step],
                        end_of_tensor=(i + step >= len(body)),
                    )
                )
            out_path.unlink(missing_ok=True)
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


# -- bundle / framing helpers round-trip (pure) --------------------------------


def test_pack_unpack_bundle_round_trips():
    from phidler.fdtd_nereid import _pack_bundle, _unpack_bundle

    files = [("job.json", b'{"out": "result.npz"}'), ("job.phidler", b"\x00\x01\x02binary\xff")]
    blob = _pack_bundle(files)
    assert _unpack_bundle(blob) == {"job.json": files[0][1], "job.phidler": files[1][1]}


def test_frame_unframe_result_round_trips_arbitrary_length():
    from phidler.fdtd_nereid import _frame_result, _unframe_result

    for npz in (b"", b"npz", b"x" * 12345, os.urandom(4096 + 3)):
        body = _frame_result(npz)
        assert len(body) % 4 == 0  # satisfies the server's float32 guard
        assert _unframe_result(body) == npz


# -- probes --------------------------------------------------------------------


def test_check_nereid_against_a_real_server(qapp, nereid_server):
    from phidler.fdtd_nereid import check_nereid

    ok, msg = check_nereid(_cfg(nereid_server))
    assert ok, msg
    assert "phidler-fdtd" in msg


def test_check_nereid_reports_a_missing_model(qapp, nereid_server):
    from phidler.fdtd_nereid import NereidConfig, check_nereid

    ok, msg = check_nereid(NereidConfig(host="127.0.0.1", port=nereid_server, model="not-a-model"))
    assert not ok
    assert "not" in msg.lower()


# -- the real round-trip -------------------------------------------------------


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
    assert ez.shape[0] > 0  # a real field movie survived the pack -> solve -> frame round-trip
    assert len(sim.grid.shape) == 3
    assert elapsed >= 0
    assert ticks and ticks[-1][0] == ticks[-1][1]  # progress streamed and reached 100%
