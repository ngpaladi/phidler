# phidler FDTD backend for nereid-server

This directory packages phidler's FDTD solver as a remote backend on
[nereid-server](https://github.com/ngpaladi/nereid-server), reached from phidler
over gRPC instead of SSH.

```
phidler app  ──gRPC──▶  nereid-server  ──stdin──▶  ml-backends/phidler-fdtd/main.py
(fdtd_nereid)            (Checkpoint→Python)        (venv: phidler[fdtd] + photonfdtd)
     ▲                                                        │
     └──── result.npz (framed float32 output tensor) ◀────────┘
```

phidler builds the *same* relocatable job bundle the local and SSH paths use
(`job.json` + `job.phidler`, see `phidler.fdtd_subprocess.write_bundle`); the
backend runs the *same* job entry point (`_run_job`) and produces the *same*
`result.npz`. Only the transport (and a thin framing) differ, so a nereid solve
is byte-for-byte what runs locally.

**Verified end-to-end against the real nereid-server** (branch
`onnx-tensorflow-backends`, `--features python`): `phidler.fdtd_nereid` round-trips
a full FDTD solve — bundle up, solve on the server, field movie back, progress
streamed. See `tests/test_nereid_loopback.py`, which replays the exact same
contract in-process.

## Contents

| File | Goes where | Purpose |
| --- | --- | --- |
| `phidler-fdtd/main.py` | `nereid-server/ml-backends/phidler-fdtd/main.py` | the backend: reads the bundle from stdin, runs the solve, writes the framed result |
| `phidler-fdtd/model_inference.textproto` | `…/phidler-fdtd/model_inference.textproto` | declares this a tensor-capable Python model (`input_shape`/`output_shape` `[-1]`, any length) |
| `phidler-fdtd/requirements.txt` | `…/phidler-fdtd/requirements.txt` | builds the model venv (phidler + photonfdtd via git URLs) |

## The contract (nereid-server's Checkpoint→Python path)

nereid-server's Python path is built for a *single numeric tensor* in and out, so
phidler's opaque payloads are wrapped — the wrapping lives entirely in `main.py`
and `phidler.fdtd_nereid` (shared `_pack_bundle` / `_frame_result` helpers), with
**no nereid-server change required**:

1. *Input.* The client packs `job.json` + `job.phidler` into one length-prefixed
   blob and sends it as a single input tensor (declared `input_shape: [-1]`, so
   any length is accepted). The server concatenates the chunk data and pipes it
   to `main.py`'s **stdin**. `main.py` reads stdin and `_unpack_bundle`s it.
2. *Invocation.* The server runs `python -u main.py` in the model dir with
   `NEREID_INPUT_SHAPE`, `NEREID_OUTPUT_PATH` (the file to produce), and
   `NEREID_OUTPUT_DTYPE`. `main.py` ignores the input shape/dtype (it reads raw
   bytes) and runs `_run_job` on the unpacked `job.json`.
3. *Live output.* `main.py`'s stdout streams back as response `chunk` text while
   it runs, so the `@@PHIDLER_PROGRESS` markers reach phidler's progress bar.
4. *Result.* `main.py` writes the `result.npz` to `NEREID_OUTPUT_PATH` as a
   framed tensor: a `float32 <n>\n` header, then the npz length-prefixed and
   zero-padded to a 4-byte boundary (`_frame_result`) — which satisfies the
   server's guard that a Checkpoint output be `float32` with a multiple-of-4 byte
   length. The server strips the header and streams the body back as
   `output_chunk`s, then a terminal `done` with the exit code. The client
   (`_unframe_result`) strips the padding to recover the exact npz.

## Deploy

On the **nereid-server** host (build it with the Python backend:
`cargo build --no-default-features --features python`):

```bash
# 1. drop in the backend (all three files)
mkdir -p ml-backends/phidler-fdtd
cp /path/to/phidler-fdtd/main.py                    ml-backends/phidler-fdtd/
cp /path/to/phidler-fdtd/model_inference.textproto  ml-backends/phidler-fdtd/
cp /path/to/phidler-fdtd/requirements.txt           ml-backends/phidler-fdtd/

# 2. register it in nereid.yaml (device/queue_capacity are required by the
#    schema but unused for Python backends; backend: python is explicit)
cat >> nereid.yaml <<'YAML'
  - name: "phidler-fdtd"
    device: "cpu"
    queue_capacity: 4
    backend: "python"
YAML

# 3. start the server — on first start it builds ml-backends/phidler-fdtd/venv
#    from requirements.txt (this pulls phidler[fdtd] + photonfdtd from GitHub
#    and can take a few minutes). It skips this if venv/ already exists, so you
#    can point venv/ at an existing phidler env to skip the build.
```

The `model_inference.textproto` is **required** — a Python model without a
declared `output_shape` is treated as text-only and can't return the result. Ours
declares `input_shape`/`output_shape` `[-1]`, so any bundle/result length passes.

For a **GPU** host, set the model's `device` to `cuda` and add the matching CuPy
wheel to `requirements.txt` (see the comment there) — `cupy-cuda12x` for an
NVIDIA/CUDA host or `cupy-rocm-5-0` for an AMD/ROCm host. photonfdtd uses only
generic CuPy array ops, so either drives the solve; it honours `use_gpu` from the
job's `FdtdParams`, set from phidler's `NereidConfig.use_gpu`.

## Use from phidler

```bash
pip install "phidler[nereid]"          # adds grpcio + protobuf
```

```python
from phidler.fdtd_nereid import NereidConfig, check_nereid, run_on_nereid

cfg = NereidConfig(host="gpubox", port=50051, model="phidler-fdtd")

ok, msg = check_nereid(cfg)            # health + model-available probe
sim_stub, result_stub, elapsed = run_on_nereid(
    document, params, region_um, cfg,
    progress_callback=lambda step, n: ...,   # driven by @@PHIDLER_PROGRESS
)
```

`run_on_nereid` returns the same `(sim_stub, result_stub, elapsed)` triple as
`fdtd_subprocess.run_in_subprocess` and `fdtd_remote.run_on_remote`, so the FDTD
window's result handler is unchanged — it's a drop-in third transport. (The app's
FDTD window / settings dialog don't call it yet; the client is ready, the UI
wiring is a separate step.)

> gRPC here is plaintext (matching nereid-server, which has no TLS). Run it on a
> trusted network or tunnel it.
