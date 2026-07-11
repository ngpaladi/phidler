"""nereid-server Python backend that runs a phidler FDTD job.

nereid-server (https://github.com/ngpaladi/nereid-server) runs a Python "model"
by executing this file inside the model's venv. Its Checkpoint→Python path is
built for a single numeric tensor: the client's input tensor arrives on **stdin**
(shape in ``NEREID_INPUT_SHAPE``), and this script writes a single framed output
tensor to the file named by ``NEREID_OUTPUT_PATH`` — a UTF-8 header line
``"<dtype> <d0>,<d1>,...\\n"`` then the raw little-endian bytes. The server only
accepts a ``float32`` output whose byte length is a multiple of 4, then strips
the header and streams the bytes back to the client.

phidler's payloads are opaque (a two-file job bundle in, an .npz out), so this
backend wraps both directions with helpers shared with the client
(``phidler.fdtd_nereid``):

  1. read the packed bundle blob from stdin and ``_unpack_bundle`` it into
     ``job.json`` + ``job.phidler`` in a scratch dir;
  2. run the *same* relocatable job entry point the local subprocess and SSH
     offload use, ``phidler.fdtd_subprocess._run_job`` — it rebuilds the design,
     runs the solve, emits ``@@PHIDLER_PROGRESS`` markers to stdout (which the
     server streams back for phidler's progress bar), and writes the result npz;
  3. ``_frame_result`` the npz (length-prefix + zero-pad to a 4-byte boundary)
     and write it under a ``float32 <n>\\n`` header to ``NEREID_OUTPUT_PATH``.

Running the identical solve path means a nereid solve is byte-for-byte what runs
locally; only the transport (and this thin framing) differ. The model must ship a
``model_inference.textproto`` declaring ``input_shape: [-1]`` and
``output_shape: [-1]`` so any length round-trips.
"""

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    output_path = os.environ.get("NEREID_OUTPUT_PATH")
    if not output_path:
        print(
            "stderr: NEREID_OUTPUT_PATH is not set — this backend must be run by "
            "nereid-server's Python Checkpoint path.",
            file=sys.stderr,
        )
        return 2

    # The whole input tensor (the packed job bundle) arrives on stdin.
    blob = sys.stdin.buffer.read()
    if not blob:
        print("stderr: no input received on stdin", file=sys.stderr)
        return 2

    # Shared pack/frame helpers, so client and backend can never drift.
    from phidler.fdtd_nereid import _frame_result, _unpack_bundle

    try:
        files = _unpack_bundle(blob)
    except Exception as exc:  # noqa: BLE001
        print(f"stderr: could not unpack the job bundle: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="phidler_nereid_job_") as work_name:
        work = Path(work_name)
        for name, data in files.items():
            (work / name).write_bytes(data)
        job_path = work / "job.json"
        if not job_path.is_file():
            print("stderr: bundle did not contain job.json", file=sys.stderr)
            return 2

        # Reuse the exact job runner the local subprocess and SSH paths use. It
        # activates the PDK, rebuilds the design, runs the solve with progress
        # markers on stdout, and writes <work>/<job["out"]> (result.npz).
        import json

        from phidler.fdtd_subprocess import _run_job

        _run_job(str(job_path))

        out_name = json.loads(job_path.read_text()).get("out", "result.npz")
        produced = work / out_name
        if not produced.is_file():
            print(f"stderr: solve finished but {produced} was not written", file=sys.stderr)
            return 1

        # Frame the npz as a float32 tensor body the server will accept, and write
        # it under the header the server's framed-tensor parser expects.
        body = _frame_result(produced.read_bytes())
        with open(output_path, "wb") as f:
            f.write(f"float32 {len(body) // 4}\n".encode("utf-8"))
            f.write(body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
