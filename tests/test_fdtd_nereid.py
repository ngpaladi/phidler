"""Remote FDTD offload over gRPC to a nereid-server. The gRPC layer (grpc + the
generated stubs) is faked via fdtd_nereid._grpc, so these exercise the
bundle→chunks→progress→reassemble logic with no real server and without needing
grpcio installed.

The fake mirrors the nereid Python-backend I/O contract (see
nereid-backend/README.md "Server-side prerequisite"): the client streams a meta
message then the bundle files as TensorChunks; the server streams stdout lines
(including @@PHIDLER_PROGRESS markers) then the result file as output_chunks then
a terminal done."""

import json
from pathlib import Path

import numpy as np
import pytest

import phidler.fdtd_nereid as fn
from phidler.fdtd_nereid import NereidConfig
from phidler.fdtd_sim import FdtdParams, SourceSpec
from phidler.model.document import LayoutDocument


# --- a fake grpc + pb2 + pb2_grpc trio -------------------------------------

class _Req:
    """A CheckpointRequest: exactly one of meta/chunk is set."""

    def __init__(self, meta=None, chunk=None):
        self.meta = meta
        self.chunk = chunk


class _Meta:
    def __init__(self, model_name="", output_file=""):
        self.model_name = model_name
        self.output_file = output_file


class _Chunk:
    def __init__(self, tensor_name="", shape=(), data=b"", chunk_index=0, end_of_tensor=False):
        self.tensor_name = tensor_name
        self.shape = list(shape)
        self.data = data
        self.chunk_index = chunk_index
        self.end_of_tensor = end_of_tensor


class _Resp:
    """A CheckpointResponse. output_chunk is None unless this is a data frame."""

    def __init__(self, chunk="", done=False, exit_code=0, output_chunk=None):
        self.chunk = chunk
        self.done = done
        self.exit_code = exit_code
        self.output_chunk = output_chunk

    def HasField(self, name):  # noqa: N802 - protobuf API name
        return name == "output_chunk" and self.output_chunk is not None


class FakePb2:
    CheckpointRequest = _Req
    CheckpointMeta = _Meta
    TensorChunk = _Chunk

    class HealthCheckRequest:
        pass

    class ViewModelsRequest:
        pass


class FakeRpcError(Exception):
    def __init__(self, code_name="UNAVAILABLE", details="boom"):
        self._code_name = code_name
        self._details = details

    def code(self):
        return type("Code", (), {"name": self._code_name})()

    def details(self):
        return self._details


class FakeGrpc:
    RpcError = FakeRpcError

    def __init__(self, channel):
        self._channel = channel

    def insecure_channel(self, target, options=None):
        self._channel.target = target
        return self._channel


class FakeChannel:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeStub:
    """Records the request stream and replays a scripted server response."""

    def __init__(self, channel, recorder):
        self._rec = recorder

    def HealthCheck(self, req, timeout=None):  # noqa: N802
        if self._rec.health_error:
            raise FakeRpcError("UNAVAILABLE", "connection refused")
        return object()

    def ViewModels(self, req, timeout=None):  # noqa: N802
        return type("R", (), {"model_names": list(self._rec.models)})()

    def Checkpoint(self, request_iter):  # noqa: N802
        # Drain and record the client's upload (meta + chunks by tensor_name).
        for req in request_iter:
            if req.meta is not None:
                self._rec.meta = req.meta
            elif req.chunk is not None:
                self._rec.uploads.setdefault(req.chunk.tensor_name, bytearray()).extend(
                    req.chunk.data
                )
        return iter(self._rec.responses)


class Recorder:
    """Holds the scripted server side and captures what the client sent."""

    def __init__(self, result_bytes, *, progress=((0, 10), (5, 10), (10, 10)),
                 exit_code=0, tail_line=None, models=("phidler-fdtd",), health_error=False):
        self.meta = None
        self.uploads = {}
        self.models = models
        self.health_error = health_error
        # Build the scripted response stream: progress lines, then the result
        # file split across two output_chunks, then done.
        responses = [_Resp(chunk=f"@@PHIDLER_PROGRESS {s} {n}") for s, n in progress]
        if tail_line is not None:
            responses.append(_Resp(chunk=tail_line))
        if result_bytes is not None:
            mid = len(result_bytes) // 2
            for i, part in enumerate((result_bytes[:mid], result_bytes[mid:])):
                responses.append(_Resp(output_chunk=_Chunk(
                    tensor_name="result.npz", shape=[len(result_bytes)], data=part,
                    chunk_index=i, end_of_tensor=(i == 1),
                )))
        responses.append(_Resp(done=True, exit_code=exit_code))
        self.responses = responses


def _install(monkeypatch, recorder):
    channel = FakeChannel()
    grpc = FakeGrpc(channel)
    pb2_grpc = type("FakePb2Grpc", (), {
        "NereidStub": lambda ch, rec=recorder: FakeStub(ch, rec),
    })
    monkeypatch.setattr(fn, "_grpc", lambda: (grpc, FakePb2, pb2_grpc))


def _fake_result_bytes(tmp_path, *, use_gpu=False, use_numba=True):
    p = tmp_path / "fake_result.npz"
    np.savez(
        p,
        ez=np.zeros((3, 10, 8), dtype=np.float32),
        x=np.arange(10), y=np.arange(8), z=np.arange(5),
        shape=np.asarray([10, 8, 5]),
        use_gpu=use_gpu, use_numba=use_numba,
    )
    # The server streams back the *framed* tensor body (post-header); the client
    # unframes it. So the scripted response must carry the framed npz, not raw.
    return fn._frame_result(p.read_bytes())


def _doc():
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 8.0, "width": 0.5})
    return doc


def _cfg(**kw):
    base = dict(host="gpubox", port=50051, model="phidler-fdtd")
    base.update(kw)
    return NereidConfig(**base)


# --- run_on_nereid ---------------------------------------------------------

def test_run_on_nereid_streams_bundle_and_parses_result(qapp, monkeypatch, tmp_path):
    rec = Recorder(_fake_result_bytes(tmp_path))
    _install(monkeypatch, rec)
    params = FdtdParams(cell_size_um=0.1, sources=(SourceSpec(x_um=-5.0, y_um=0.0),))

    sim_stub, result_stub, elapsed = fn.run_on_nereid(_doc(), params, None, _cfg())

    # parsed into exactly the stub contract the display reads
    assert sim_stub.grid.shape == (10, 8, 5)
    assert result_stub.fields["field"]["Ez"].shape == (3, 10, 8)
    assert sim_stub.use_numba is True
    assert elapsed >= 0

    # the client sent the right meta and uploaded the bundle as one packed tensor
    assert rec.meta.model_name == "phidler-fdtd"
    assert rec.meta.output_file == "result.npz"
    assert set(rec.uploads) == {"input"}
    files = fn._unpack_bundle(bytes(rec.uploads["input"]))
    assert set(files) == {"job.json", "job.phidler"}
    job = json.loads(files["job.json"])
    assert job["project"] == "job.phidler" and "params" in job and job["out"] == "result.npz"
    assert len(files["job.phidler"]) > 0


def test_run_on_nereid_forwards_progress(qapp, monkeypatch, tmp_path):
    rec = Recorder(_fake_result_bytes(tmp_path))
    _install(monkeypatch, rec)
    ticks = []
    fn.run_on_nereid(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg(),
                     progress_callback=lambda i, n: ticks.append((i, n)))
    assert ticks == [(0, 10), (5, 10), (10, 10)]  # markers parsed, not treated as errors


def test_run_on_nereid_uses_backend_gpu_flag_from_result(qapp, monkeypatch, tmp_path):
    """The displayed backend comes from what the server actually ran (the
    returned npz), not what was requested."""
    rec = Recorder(_fake_result_bytes(tmp_path, use_gpu=True, use_numba=False))
    _install(monkeypatch, rec)
    sim_stub, _, _ = fn.run_on_nereid(_doc(), FdtdParams(cell_size_um=0.1, use_gpu=True), None,
                                      _cfg(use_gpu=True))
    assert sim_stub.use_gpu is True


def test_run_on_nereid_propagates_backend_failure(qapp, monkeypatch, tmp_path):
    # non-zero exit, no result streamed, a stderr tail line to surface
    rec = Recorder(None, exit_code=1, tail_line="stderr: RuntimeError: boom")
    _install(monkeypatch, rec)
    with pytest.raises(RuntimeError) as exc:
        fn.run_on_nereid(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg())
    assert "boom" in str(exc.value)


def test_run_on_nereid_raises_on_rpc_error(qapp, monkeypatch):
    rec = Recorder(None)

    def boom_stub(ch, rec=rec):
        s = FakeStub(ch, rec)
        s.Checkpoint = lambda request_iter: (_ for _ in ()).throw(FakeRpcError("UNAVAILABLE", "no route"))
        return s

    channel = FakeChannel()
    grpc = FakeGrpc(channel)
    pb2_grpc = type("FakePb2Grpc", (), {"NereidStub": boom_stub})
    monkeypatch.setattr(fn, "_grpc", lambda: (grpc, FakePb2, pb2_grpc))
    with pytest.raises(RuntimeError) as exc:
        fn.run_on_nereid(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg())
    assert "no route" in str(exc.value)


def test_run_on_nereid_unconfigured_raises(qapp):
    with pytest.raises(RuntimeError):
        fn.run_on_nereid(_doc(), FdtdParams(), None, NereidConfig())


# --- check_nereid ----------------------------------------------------------

def test_check_nereid_ok(monkeypatch, tmp_path):
    rec = Recorder(_fake_result_bytes(tmp_path), models=("phidler-fdtd", "model3"))
    _install(monkeypatch, rec)
    ok, msg = fn.check_nereid(_cfg())
    assert ok is True
    assert "gpubox:50051" in msg and "phidler-fdtd" in msg


def test_check_nereid_model_missing(monkeypatch, tmp_path):
    rec = Recorder(_fake_result_bytes(tmp_path), models=("model3",))
    _install(monkeypatch, rec)
    ok, msg = fn.check_nereid(_cfg())
    assert ok is False
    assert "not" in msg.lower() and "model3" in msg


def test_check_nereid_connection_error(monkeypatch, tmp_path):
    rec = Recorder(None, health_error=True)
    _install(monkeypatch, rec)
    ok, msg = fn.check_nereid(_cfg())
    assert ok is False
    assert "connection refused" in msg


def test_check_nereid_not_configured_does_not_connect(monkeypatch):
    called = []
    monkeypatch.setattr(fn, "_grpc", lambda: called.append(True) or (None, None, None))
    ok, msg = fn.check_nereid(NereidConfig())
    assert ok is False
    assert not called  # never attempted to import grpc / connect
