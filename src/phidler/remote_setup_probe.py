"""Detect a machine's GPU/accelerator hardware and install the ideal photonfdtd
backend for it.

This runs *on the remote host* during "Connect & set up" (phidler invokes
``<remote_python> -m phidler.remote_setup_probe --backend <choice> --install``),
but it's a self-contained, stdlib-only script you can also run by hand on any
machine to see what it would pick:

    python -m phidler.remote_setup_probe            # detect + recommend (dry run)
    python -m phidler.remote_setup_probe --install  # ...and install it

It prints machine-readable markers the caller parses:

    PHIDLER_DETECT=<one-line hardware summary>
    PHIDLER_RECOMMEND=<cpu|jax|cupy>     # the concrete backend to run with
    PHIDLER_INSTALL=<ok|skipped|failed>

The recommendation is what makes an "Auto" setup resolve to a concrete backend:
JAX is the preferred GPU path (photonfdtd 0.9 runs it on the GPU via XLA), with
CuPy as the fallback where JAX can't (CUDA 11, ROCm), and CPU/Numba when there's
no GPU. Numba ships with photonfdtd's own install, so the CPU path needs nothing
extra.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Hardware:
    """What we could learn about the machine's accelerators."""

    gpu: str | None = None  # "nvidia" | "amd" | None
    cuda_major: int | None = None  # max CUDA the NVIDIA driver supports (from nvidia-smi)
    gpu_name: str = ""
    detail: str = ""  # human-readable one-liner

    def summary(self) -> str:
        if self.gpu == "nvidia":
            cuda = f"CUDA {self.cuda_major}" if self.cuda_major else "CUDA version unknown"
            return f"NVIDIA GPU ({self.gpu_name or '?'}), {cuda}"
        if self.gpu == "amd":
            return f"AMD/ROCm GPU ({self.gpu_name or '?'})"
        return "no GPU detected (CPU only)"


@dataclass
class Recommendation:
    """The chosen backend and the pip package (if any) that enables it."""

    backend: str  # "cpu" | "jax" | "cupy"
    pip_spec: str | None  # e.g. "jax[cuda12]"; None when nothing extra is needed
    reason: str


def _run(cmd: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess | None:
    """Run a detection command, returning the CompletedProcess or None if the
    tool is missing / errors / times out. Never raises."""
    if shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def detect_hardware() -> Hardware:
    """Probe for an NVIDIA or AMD GPU. NVIDIA is read from ``nvidia-smi`` (which
    also reports the max CUDA version the installed driver supports); AMD from
    ``rocminfo`` / ``rocm-smi`` / an /opt/rocm install."""
    smi = _run(["nvidia-smi"])
    if smi is not None and smi.returncode == 0:
        out = smi.stdout or ""
        cuda_major = None
        m = re.search(r"CUDA Version:\s*(\d+)", out)
        if m:
            cuda_major = int(m.group(1))
        name = ""
        nm = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        if nm is not None and nm.returncode == 0 and nm.stdout.strip():
            name = nm.stdout.strip().splitlines()[0].strip()
        return Hardware(gpu="nvidia", cuda_major=cuda_major, gpu_name=name)

    # AMD: any of these being present is a good signal of a ROCm stack.
    for probe in (["rocminfo"], ["rocm-smi"]):
        proc = _run(probe)
        if proc is not None and proc.returncode == 0:
            name = ""
            mm = re.search(r"Marketing Name:\s*(.+)", proc.stdout or "")
            if mm:
                name = mm.group(1).strip()
            return Hardware(gpu="amd", gpu_name=name)
    if os.path.isdir("/opt/rocm"):
        return Hardware(gpu="amd")

    return Hardware()


def decide_backend(hw: Hardware, requested: str = "auto") -> Recommendation:
    """Pick the concrete backend + pip package for this hardware and the user's
    request. ``requested`` is "auto" (let this choose), "cpu" or "jax" (the
    GPU). photonfdtd (>=0.13) steps only on JAX, which it installs itself, so
    the only choice left is whether to add JAX's CUDA build so runs go to the
    GPU. A legacy "cupy" request (CuPy was removed in photonfdtd 0.13) is
    treated as a GPU request."""
    requested = (requested or "auto").lower()
    if requested == "cupy":
        requested = "jax"

    if requested == "cpu":
        return Recommendation("cpu", None, "JAX on the CPU (ships with photonfdtd; nothing extra to install).")

    # "jax" or "auto": use the GPU where JAX's CUDA wheels can.
    if hw.gpu == "nvidia":
        if hw.cuda_major is not None and hw.cuda_major >= 12:
            return Recommendation("jax", "jax[cuda12]", f"NVIDIA GPU with CUDA {hw.cuda_major} → JAX runs on the GPU via XLA.")
        return Recommendation(
            "cpu", None,
            f"NVIDIA GPU with CUDA {hw.cuda_major or 'unknown'} → JAX's GPU build needs a CUDA 12 "
            "driver, so runs stay on the CPU (update the driver, then set up again).",
        )
    if hw.gpu == "amd":
        return Recommendation("cpu", None, "AMD/ROCm GPU → no supported JAX GPU build is installed automatically; using JAX on the CPU.")
    return Recommendation("cpu", None, "No GPU detected → JAX on the CPU.")


def _pip_install(pip_spec: str, python: str) -> bool:
    """Install ``pip_spec`` into ``python``'s environment, streaming pip's output
    to stdout. The spec is passed as a single argv element, so brackets like
    ``jax[cuda12]`` stay literal without a shell."""
    cmd = [python, "-m", "pip", "install", pip_spec]
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(cmd)
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"pip install failed: {exc}", flush=True)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect GPU hardware and install the ideal photonfdtd backend.")
    parser.add_argument("--backend", default="auto", choices=["auto", "cpu", "jax", "cupy"],
                        help="Which backend to set up (auto = pick the best for this hardware).")
    parser.add_argument("--install", action="store_true", help="Actually install (default is a dry run).")
    parser.add_argument("--python", default=sys.executable,
                        help="Interpreter to install into (default: the one running this).")
    args = parser.parse_args(argv)

    hw = detect_hardware()
    rec = decide_backend(hw, args.backend)

    print(f"PHIDLER_DETECT={hw.summary()}", flush=True)
    print(f"Recommended backend: {rec.backend} — {rec.reason}", flush=True)
    print(f"PHIDLER_RECOMMEND={rec.backend}", flush=True)

    if rec.pip_spec is None:
        print("Nothing extra to install for this backend.", flush=True)
        print("PHIDLER_INSTALL=skipped", flush=True)
        return 0

    print(f"Ideal package: {rec.pip_spec}", flush=True)
    if not args.install:
        print("(dry run — pass --install to install it)", flush=True)
        print("PHIDLER_INSTALL=skipped", flush=True)
        return 0

    ok = _pip_install(rec.pip_spec, args.python)
    print(f"PHIDLER_INSTALL={'ok' if ok else 'failed'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
