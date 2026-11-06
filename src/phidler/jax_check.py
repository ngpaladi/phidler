"""Check whether JAX is using proper GPU bindings.

photonfdtd steps on JAX, which runs on the GPU only when a CUDA build of jaxlib
(the ``jax-cuda12-plugin``) is installed *and* loads. When the plugin is
missing, or installed but unable to load its CUDA libraries, JAX quietly falls
back to the CPU and only logs a warning: the run works, just many times slower.
This module diagnoses which case you're in.

The check runs in a fresh child process, so it sees what a clean JAX start
sees (JAX picks its backend once per process; the app's own JAX may already be
initialized) and so its log output can be captured. Run it from a shell with
``python -m phidler.jax_check``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# Runs in the child. Prints one JSON line prefixed with the marker; everything
# JAX logs goes to stderr, which the parent captures for the report.
_CHILD = r"""
import json, time, importlib.metadata as md
info = {"jax": None, "jaxlib": None, "plugins": {}, "backend": None, "devices": [],
        "gpu_compute": None, "gpu_compute_ms": None, "error": None}
for name in ("jax-cuda12-plugin", "jax-cuda12-pjrt", "jax-cuda13-plugin", "jax-cuda13-pjrt",
             "jax-rocm60-plugin", "jax-rocm7-plugin"):
    try:
        info["plugins"][name] = md.version(name)
    except Exception:
        pass
try:
    import jax, jaxlib
    info["jax"] = jax.__version__
    info["jaxlib"] = getattr(jaxlib, "__version__", None)
    info["backend"] = jax.default_backend()
    info["devices"] = [f"{d.platform}:{d.device_kind}" for d in jax.devices()]
    gpus = [d for d in jax.devices() if d.platform in ("gpu", "cuda", "rocm")]
    if gpus:
        import jax.numpy as jnp
        x = jax.device_put(jnp.ones((1024, 1024), jnp.float32), gpus[0])
        (x @ x).block_until_ready()
        t = time.perf_counter()
        y = (x @ x).block_until_ready()
        info["gpu_compute_ms"] = (time.perf_counter() - t) * 1e3
        info["gpu_compute"] = bool(float(y[0, 0]) == 1024.0)
except Exception as exc:
    info["error"] = f"{type(exc).__name__}: {exc}"
print("@@JAXCHECK " + json.dumps(info))
"""

_MARKER = "@@JAXCHECK "

# stderr lines worth showing: JAX's own fallback/plugin warnings.
_INTERESTING = re.compile(
    r"(unable to (load|initialize)|cuda|cudnn|cusparse|cublas|nvidia|plugin|falling back|"
    r"no gpu|gpu|backend|error)", re.I)


@dataclass
class JaxGpuReport:
    """The outcome of :func:`check_jax_gpu`. ``status`` is one of:

    * ``ok``: JAX found a GPU and computed on it.
    * ``no_gpu``: no NVIDIA GPU on this machine (nvidia-smi absent or failing),
      so CPU is expected.
    * ``plugin_missing``: there is an NVIDIA GPU but no CUDA build of JAX.
    * ``driver_too_old``: the NVIDIA driver supports an older CUDA than JAX's
      GPU build needs (12).
    * ``plugin_failed``: the CUDA plugin is installed but JAX still runs on the
      CPU, typically because it couldn't load its CUDA libraries.
    * ``gpu_compute_failed``: JAX lists a GPU but a test computation on it failed.
    * ``jax_missing``: JAX itself doesn't import.
    """

    status: str
    summary: str
    advice: str = ""
    python: str = ""
    jax: str | None = None
    jaxlib: str | None = None
    plugins: dict = field(default_factory=dict)
    backend: str | None = None
    devices: list = field(default_factory=list)
    gpu_name: str = ""
    driver_cuda: int | None = None
    gpu_compute_ms: float | None = None
    log: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def text(self) -> str:
        """The report as plain text, for a dialog or a terminal."""
        plugins = ", ".join(f"{k} {v}" for k, v in self.plugins.items()) or "none"
        lines = [
            self.summary,
            "",
            f"Python:          {self.python}",
            f"jax / jaxlib:    {self.jax or 'not installed'} / {self.jaxlib or '-'}",
            f"GPU plugins:     {plugins}",
            f"Default backend: {self.backend or '-'}",
            f"Devices:         {', '.join(self.devices) or '-'}",
            f"NVIDIA GPU:      {self.gpu_name or 'none detected'}"
            + (f" (driver supports CUDA {self.driver_cuda})" if self.driver_cuda else ""),
        ]
        if self.gpu_compute_ms is not None:
            lines.append(f"GPU test:        1024x1024 matmul in {self.gpu_compute_ms:.2f} ms")
        if self.advice:
            lines += ["", self.advice]
        if self.log:
            lines += ["", "What JAX logged while starting:"] + [f"  {ln}" for ln in self.log]
        return "\n".join(lines)


def _nvidia() -> tuple[str, int | None] | None:
    """(GPU name, driver's max CUDA major) from nvidia-smi, or None when there
    is no NVIDIA GPU. Inside a sandbox such as the Flatpak there's no
    nvidia-smi, so fall back to the driver's device nodes (name and CUDA
    version then unknown)."""
    if shutil.which("nvidia-smi") is None:
        if os.path.exists("/dev/nvidiactl") or os.path.exists("/dev/nvidia0"):
            return ("NVIDIA GPU (nvidia-smi not available here)", None)
        return None
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        m = re.search(r"CUDA Version:\s*(\d+)", out.stdout)
        name = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=15).stdout.strip()
        return (name.splitlines()[0].strip() if name else "NVIDIA GPU", int(m.group(1)) if m else None)
    except Exception:
        return None


def check_jax_gpu(python: str | None = None, timeout: float = 180.0) -> JaxGpuReport:
    """Start JAX in a child ``python`` (default: this interpreter) and report
    whether it runs on the GPU, and if not, why. Never raises."""
    python = python or sys.executable
    nv = _nvidia()
    gpu_name, driver_cuda = nv if nv else ("", None)
    try:
        # No preallocation: the test needs a few MB, and the app (or another
        # run) may already hold most of the GPU's memory.
        env = {**os.environ, "XLA_PYTHON_CLIENT_PREALLOCATE": "false"}
        proc = subprocess.run([python, "-c", _CHILD], capture_output=True, text=True,
                              timeout=timeout, env=env)
        out, err = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return JaxGpuReport("jax_missing", "JAX didn't finish starting within "
                            f"{timeout:.0f} s.", python=python, gpu_name=gpu_name, driver_cuda=driver_cuda)
    line = next((ln for ln in out.splitlines() if ln.startswith(_MARKER)), None)
    log = [ln.strip() for ln in err.splitlines() if ln.strip() and _INTERESTING.search(ln)][-12:]
    info = json.loads(line[len(_MARKER):]) if line else {"error": (err.strip().splitlines() or ["no output"])[-1]}
    r = JaxGpuReport(
        status="", summary="", python=python, jax=info.get("jax"), jaxlib=info.get("jaxlib"),
        plugins=info.get("plugins") or {}, backend=info.get("backend"), devices=info.get("devices") or [],
        gpu_name=gpu_name, driver_cuda=driver_cuda, gpu_compute_ms=info.get("gpu_compute_ms"), log=log,
    )
    has_gpu_device = any(not d.startswith("cpu") for d in r.devices)
    cuda_plugin = any("cuda" in k for k in r.plugins)

    if r.jax is None:
        r.status, r.summary = "jax_missing", "JAX doesn't import in this environment."
        r.advice = f"Error: {info.get('error')}\nInstall it with: {python} -m pip install jax"
    elif has_gpu_device and info.get("gpu_compute"):
        r.status = "ok"
        r.summary = f"OK: JAX is using the GPU ({', '.join(d for d in r.devices if not d.startswith('cpu'))})."
    elif has_gpu_device:
        r.status, r.summary = "gpu_compute_failed", "JAX lists a GPU, but a test computation on it failed."
        r.advice = f"Error: {info.get('error')}"
    elif nv is None:
        r.status = "no_gpu"
        r.summary = "No NVIDIA GPU found on this machine, so JAX runs on the CPU (expected)."
    elif driver_cuda is not None and driver_cuda < 12:
        r.status = "driver_too_old"
        r.summary = f"JAX runs on the CPU: the NVIDIA driver only supports CUDA {driver_cuda}."
        r.advice = "JAX's GPU build needs a driver supporting CUDA 12 or newer. Update the NVIDIA driver."
    elif not cuda_plugin:
        r.status = "plugin_missing"
        r.summary = "JAX runs on the CPU: there's an NVIDIA GPU, but JAX's CUDA build isn't installed."
        r.advice = f'Install it with: {python} -m pip install "jax[cuda12]=={r.jax}"'
    else:
        r.status = "plugin_failed"
        r.summary = ("JAX runs on the CPU even though its CUDA plugin is installed: the plugin "
                     "failed to load (see the log below).")
        r.advice = ("The usual cause is that the CUDA libraries the plugin needs (cuSPARSE, cuDNN, ...) "
                    "aren't found. If they were installed as pip 'nvidia-*' wheels into a different "
                    "site-packages, add their lib directories to LD_LIBRARY_PATH, or reinstall with "
                    f'{python} -m pip install --force-reinstall "jax[cuda12]=={r.jax}".')
    return r


if __name__ == "__main__":
    report = check_jax_gpu()
    print(report.text())
    sys.exit(0 if report.status in ("ok", "no_gpu") else 1)
