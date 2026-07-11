"""Run an FDTD simulation on a remote nereid-server over gRPC.

This is a third transport alongside the local child process (fdtd_subprocess)
and the SSH offload (fdtd_remote). All three build the *same* relocatable job
bundle (a saved .phidler + a job.json of basenames, see
fdtd_subprocess.write_bundle) and read back the *same* small result.npz, so the
FDTD window's result handler is unchanged: ``run_on_nereid`` returns the same
``(sim_stub, result_stub, elapsed)`` shape as ``run_in_subprocess`` /
``run_on_remote``.

Where SSH copies the bundle and shells out, this streams it to a running
nereid-server (https://github.com/ngpaladi/nereid-server) over gRPC. nereid runs
a Python "model" — here the ``phidler-fdtd`` backend (ml-backends/phidler-fdtd/
main.py) — inside its own venv and streams the model's stdout back to us. The
backend reuses ``phidler.fdtd_subprocess._run_job`` verbatim, so what runs on the
server is byte-for-byte what runs locally; only the transport differs.

Wire contract. nereid-server's Checkpoint→Python path is a *single numeric
tensor* in, a *single float32 tensor* out (the model reads its input tensor on
stdin and writes a framed output tensor). phidler's payloads are opaque (a
two-file job bundle in, an .npz out), so both directions are wrapped:

  * client → server: a ``CheckpointMeta`` (model name), then the job bundle
    packed into one opaque blob (_pack_bundle) sent as a single input tensor's
    ``TensorChunk``s. The server concatenates the chunk data and pipes it to the
    backend's stdin.
  * server → client: the model's stdout/stderr as ``chunk`` text lines (we parse
    the ``@@PHIDLER_PROGRESS`` markers for the progress bar), then the output
    tensor body as ``output_chunk``s (the server has already stripped the
    backend's ``float32 <n>\\n`` frame header), then a terminal ``done`` with the
    exit code. The body is the .npz length-prefixed and zero-padded to a 4-byte
    boundary (_frame_result); the client strips that to recover the exact .npz.

The backend (nereid-backend/phidler-fdtd/main.py) does the mirror image:
_unpack_bundle from stdin, run the solve, _frame_result the .npz. The model
declares ``input_shape: [-1]`` / ``output_shape: [-1]`` so any length passes.

gRPC and the generated stubs are an optional dependency (the ``nereid`` extra):
every entry point imports them lazily so plain phidler keeps working without
grpcio installed.
"""

from __future__ import annotations

import collections
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .fdtd_sim import FdtdParams
from .fdtd_subprocess import _ERROR_TAIL_LINES, load_result_npz, parse_progress_line, write_bundle

# The model folder name under nereid's ml_backends_path; must match the backend
# directory (ml-backends/phidler-fdtd/) and the server's nereid.yaml entry.
DEFAULT_MODEL = "phidler-fdtd"
# nereid-server's default bind port (see nereid.yaml.example "[::1]:50051").
DEFAULT_PORT = 50051
# The output basename the backend writes and the server streams back. Matches
# the "out" field write_bundle records in job.json, so the two never drift.
_OUTPUT_FILE = "result.npz"
# The two bundle files streamed up, by basename. write_bundle always produces
# exactly these; _pack_bundle wraps them into one opaque tensor for the wire.
_BUNDLE_FILES = ("job.json", "job.phidler")
# Outbound chunk size for the (small) bundle. Well under gRPC's 4 MB default
# message limit; the result comes back in the server's own 64 KB chunks.
_UPLOAD_CHUNK_BYTES = 256 * 1024
# nereid-server's Checkpoint→Python path is built for a single numeric tensor:
# one input tensor arrives on the model's stdin (with input_shape declared
# [-1]), and the model writes one framed output tensor the server only accepts
# as float32 with a byte length that's a multiple of 4. phidler carries an
# opaque bundle (two files) and an opaque result (an .npz), so both directions
# are wrapped:
#   * the two bundle files are packed into one length-prefixed blob (_pack_bundle)
#     and sent as a single tensor named "input";
#   * the .npz is length-prefixed and zero-padded to a 4-byte boundary
#     (_frame_result), labelled float32 by the backend, so it satisfies the
#     server's guards; the client strips the padding back off (_unframe_result).
# See nereid-backend/phidler-fdtd/main.py, which does the mirror-image unpack/frame.
_INPUT_TENSOR = "input"
_BUNDLE_MAGIC = b"PHB1"
# Short deadline for the lightweight readiness probes (HealthCheck/ViewModels),
# so a dead/wrong host reports quickly instead of hanging the dialog.
_PROBE_TIMEOUT_S = 15.0
# Generous per-message ceiling so a large bundle or result chunk can't trip
# gRPC's default 4 MB limit (each message is far smaller by design).
_CHANNEL_OPTIONS = [
    ("grpc.max_send_message_length", 64 * 1024 * 1024),
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),
]


@dataclass
class NereidConfig:
    """Where the nereid-server is and which model serves FDTD.

    ``host``/``port`` address the gRPC server; ``model`` is the folder name of
    the phidler FDTD backend configured there (default ``phidler-fdtd``).
    ``use_gpu`` requests the server's GPU backend independently of the local
    machine — the backend honours it the same way the SSH path does, through the
    saved job's FdtdParams. Plaintext gRPC, like nereid itself (no TLS)."""

    host: str = ""
    port: int = DEFAULT_PORT
    model: str = DEFAULT_MODEL
    use_gpu: bool = False

    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def is_configured(self) -> bool:
        return bool(self.host and self.port and self.model)


def _grpc():
    """Import grpc and the generated stubs lazily, with a friendly error if the
    optional ``nereid`` extra isn't installed. Keeps importing this module cheap
    and side-effect-free for installs that never use the gRPC transport."""
    try:
        import grpc

        from ._nereid import pb2, pb2_grpc
    except ImportError as exc:  # pragma: no cover - exercised via the message
        raise RuntimeError(
            "The nereid (gRPC) transport needs grpcio. Install it with "
            "`pip install \"phidler[nereid]\"` (or `pip install grpcio`)."
        ) from exc
    return grpc, pb2, pb2_grpc


def check_nereid(cfg: NereidConfig) -> tuple[bool, str]:
    """Quick readiness probe: is the server up and is ``cfg.model`` configured?
    Returns (ok, message). Never raises — connection errors come back as
    (False, <reason>) so the dialog can show them."""
    if not cfg.is_configured():
        return False, "Set a server host and FDTD model name first."
    try:
        grpc, pb2, pb2_grpc = _grpc()
    except RuntimeError as exc:
        return False, str(exc)
    try:
        with grpc.insecure_channel(cfg.target(), options=_CHANNEL_OPTIONS) as channel:
            stub = pb2_grpc.NereidStub(channel)
            stub.HealthCheck(pb2.HealthCheckRequest(), timeout=_PROBE_TIMEOUT_S)
            models = list(
                stub.ViewModels(pb2.ViewModelsRequest(), timeout=_PROBE_TIMEOUT_S).model_names
            )
    except grpc.RpcError as exc:
        return False, f"{exc.code().name}: {exc.details() or 'gRPC error'}"
    except Exception as exc:  # bad address, etc.
        return False, str(exc)
    if cfg.model not in models:
        available = ", ".join(models) or "(none)"
        return False, (
            f"Connected to {cfg.target()}, but model '{cfg.model}' is not "
            f"configured on the server. Available models: {available}."
        )
    return True, f"Connected to {cfg.target()}: model '{cfg.model}' is available."


def _pack_bundle(files: list[tuple[str, bytes]]) -> bytes:
    """Pack the (name, bytes) bundle files into one opaque, length-prefixed blob:
    magic, file count, then per file ``u32 name_len | name | u64 data_len | data``.
    _unpack_bundle (in the backend's main.py) reverses it."""
    out = bytearray(_BUNDLE_MAGIC)
    out += struct.pack("<I", len(files))
    for name, data in files:
        name_bytes = name.encode("utf-8")
        out += struct.pack("<I", len(name_bytes)) + name_bytes
        out += struct.pack("<Q", len(data)) + data
    return bytes(out)


def _unpack_bundle(blob: bytes) -> dict[str, bytes]:
    """Inverse of _pack_bundle. Used by the backend to recover job.json / job.phidler."""
    if blob[:4] != _BUNDLE_MAGIC:
        raise ValueError("not a phidler bundle blob (bad magic)")
    offset = 4
    (count,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    files: dict[str, bytes] = {}
    for _ in range(count):
        (name_len,) = struct.unpack_from("<I", blob, offset)
        offset += 4
        name = blob[offset : offset + name_len].decode("utf-8")
        offset += name_len
        (data_len,) = struct.unpack_from("<Q", blob, offset)
        offset += 8
        files[name] = blob[offset : offset + data_len]
        offset += data_len
    return files


def _frame_result(npz: bytes) -> bytes:
    """The tensor *body* the backend writes for an .npz result: an 8-byte true
    length, the npz bytes, then zero padding up to a 4-byte boundary — so the
    server (which only accepts a float32 output whose byte length is a multiple
    of 4) is satisfied. The backend prepends the ``float32 <n>\\n`` header; the
    server strips that header and streams this body back."""
    payload = struct.pack("<Q", len(npz)) + npz
    payload += b"\x00" * (-len(payload) % 4)
    return payload


def _unframe_result(payload: bytes) -> bytes:
    """Recover the npz bytes from the framed tensor body (inverse of _frame_result)."""
    if len(payload) < 8:
        raise ValueError("nereid result payload too short to be framed")
    (true_len,) = struct.unpack_from("<Q", payload, 0)
    return payload[8 : 8 + true_len]


def _iter_upload_chunks(pb2, name: str, data: bytes) -> Iterator:
    """Yield one tensor_name=``name`` TensorChunk per ``_UPLOAD_CHUNK_BYTES`` of
    ``data`` (at least one, so an empty tensor still arrives). Every chunk of one
    tensor must declare the same shape, so each carries the full ``[len(data)]``;
    the server concatenates the chunk data in arrival order."""
    total = (len(data) + _UPLOAD_CHUNK_BYTES - 1) // _UPLOAD_CHUNK_BYTES or 1
    for index in range(total):
        start = index * _UPLOAD_CHUNK_BYTES
        yield pb2.CheckpointRequest(
            chunk=pb2.TensorChunk(
                tensor_name=name,
                shape=[len(data)],
                data=data[start : start + _UPLOAD_CHUNK_BYTES],
                chunk_index=index,
                end_of_tensor=(index + 1 == total),
            )
        )


def _request_stream(pb2, cfg: NereidConfig, blob: bytes) -> Iterator:
    """The client→server request stream: the metadata message first (required by
    the server), then the packed bundle as one tensor's run of TensorChunks."""
    yield pb2.CheckpointRequest(
        meta=pb2.CheckpointMeta(model_name=cfg.model, output_file=_OUTPUT_FILE)
    )
    yield from _iter_upload_chunks(pb2, _INPUT_TENSOR, blob)


def run_on_nereid(
    document, params: FdtdParams, region_um=None, cfg: NereidConfig | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[Any, Any, float]:
    """Build the job bundle locally, stream it to the nereid-server, and bring
    the result back — returning ``(sim_stub, result_stub, elapsed)`` identical in
    shape to run_in_subprocess / run_on_remote so the caller is none the wiser.
    ``progress_callback(step, n_steps)``, if given, is called as the backend
    streams ``@@PHIDLER_PROGRESS`` markers back over gRPC.

    Raises RuntimeError (with the server's error message) on any transport or
    solve failure."""
    if cfg is None or not cfg.is_configured():
        raise RuntimeError("No nereid server is configured (set a host and model name).")
    grpc, pb2, pb2_grpc = _grpc()

    with tempfile.TemporaryDirectory(prefix="phidler_nereid_") as tmp_name:
        tmp = Path(tmp_name)
        write_bundle(tmp, document, params, region_um)
        blob = _pack_bundle([(name, (tmp / name).read_bytes()) for name in _BUNDLE_FILES])

        # Last few non-progress stdout/stderr lines, kept for an error message.
        tail: collections.deque[str] = collections.deque(maxlen=_ERROR_TAIL_LINES)
        result_bytes = bytearray()
        exit_code: int | None = None

        t0 = time.time()
        try:
            with grpc.insecure_channel(cfg.target(), options=_CHANNEL_OPTIONS) as channel:
                stub = pb2_grpc.NereidStub(channel)
                for resp in stub.Checkpoint(_request_stream(pb2, cfg, blob)):
                    line = resp.chunk
                    if line:
                        prog = parse_progress_line(line)
                        if prog is not None:
                            if progress_callback is not None:
                                progress_callback(prog[0], prog[1])
                        else:
                            tail.append(line)
                    if resp.HasField("output_chunk"):
                        result_bytes += resp.output_chunk.data
                    if resp.done:
                        exit_code = resp.exit_code
        except grpc.RpcError as exc:
            raise RuntimeError(
                f"nereid simulation failed: {exc.code().name}: {exc.details() or 'gRPC error'}"
            ) from exc
        elapsed = time.time() - t0

        if exit_code is None:
            raise RuntimeError("nereid stream ended without a completion marker.")
        if exit_code != 0:
            detail = (tail[-1] if tail else "") or f"backend exited with code {exit_code}"
            raise RuntimeError(f"nereid simulation failed: {detail}")
        if not result_bytes:
            detail = (tail[-1] if tail else "") or "no result produced"
            raise RuntimeError(f"nereid run produced no result. {detail}")

        # The server streamed back the tensor body (header already stripped);
        # strip our own length-prefix/padding to recover the exact .npz bytes.
        npz = _unframe_result(bytes(result_bytes))
        out_path = tmp / _OUTPUT_FILE
        out_path.write_bytes(npz)
        sim_stub, result_stub = load_result_npz(out_path)
        return sim_stub, result_stub, elapsed
