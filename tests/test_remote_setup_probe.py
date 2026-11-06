"""The remote hardware probe: hardware detection, the ideal-backend decision
matrix, and the runnable script's output markers."""

from __future__ import annotations

import subprocess

import phidler.remote_setup_probe as probe
from phidler.remote_setup_probe import Hardware, decide_backend, detect_hardware, main


def _cp(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# -- decision matrix -----------------------------------------------------------


def test_auto_prefers_jax_on_cuda12():
    r = decide_backend(Hardware("nvidia", 12, "A100"), "auto")
    assert r.backend == "jax" and r.pip_spec == "jax[cuda12]"


def test_auto_stays_on_cpu_on_cuda11():
    # JAX's GPU wheels need a CUDA 12 driver and CuPy is gone (photonfdtd 0.13),
    # so an older driver runs JAX on the CPU and installs nothing extra.
    r = decide_backend(Hardware("nvidia", 11, "V100"), "auto")
    assert r.backend == "cpu" and r.pip_spec is None
    assert "CUDA 12" in r.reason


def test_auto_stays_on_cpu_on_amd():
    r = decide_backend(Hardware("amd", None, "MI250"), "auto")
    assert r.backend == "cpu" and r.pip_spec is None


def test_auto_uses_cpu_with_no_gpu():
    r = decide_backend(Hardware(None), "auto")
    assert r.backend == "cpu" and r.pip_spec is None


def test_explicit_jax_matches_auto():
    """An explicit GPU request picks JAX's CUDA build where it can run and
    otherwise degrades to the CPU rather than installing a build that can't."""
    def pick(hw):
        r = decide_backend(hw, "jax")
        return r.backend, r.pip_spec

    assert pick(Hardware("nvidia", 12)) == ("jax", "jax[cuda12]")
    assert pick(Hardware("nvidia", 11)) == ("cpu", None)
    assert pick(Hardware("amd")) == ("cpu", None)
    assert pick(Hardware(None)) == ("cpu", None)


def test_legacy_cupy_request_is_treated_as_jax():
    for hw in (Hardware("nvidia", 12), Hardware("nvidia", 11), Hardware("amd"), Hardware(None)):
        assert decide_backend(hw, "cupy") == decide_backend(hw, "jax")
    assert decide_backend(Hardware("nvidia", 12), "CuPy").backend == "jax"  # case-insensitive


def test_cpu_installs_nothing_even_with_a_gpu():
    r = decide_backend(Hardware("nvidia", 12), "cpu")
    assert r.backend == "cpu" and r.pip_spec is None
    assert decide_backend(Hardware(None), None).backend == "cpu"  # None means auto


# -- hardware detection --------------------------------------------------------


def test_detect_nvidia_reads_cuda_version(monkeypatch):
    def fake_run(cmd, timeout=15.0):
        if cmd[:1] == ["nvidia-smi"] and "--query-gpu=name" in cmd:
            return _cp("NVIDIA GeForce RTX 4080\n")
        if cmd[:1] == ["nvidia-smi"]:
            return _cp("| NVIDIA-SMI 550.00  Driver Version: 550.00  CUDA Version: 12.4 |\n")
        return None

    monkeypatch.setattr(probe, "_run", fake_run)
    hw = detect_hardware()
    assert hw.gpu == "nvidia" and hw.cuda_major == 12
    assert "RTX 4080" in hw.gpu_name


def test_detect_amd_via_rocminfo(monkeypatch):
    def fake_run(cmd, timeout=15.0):
        if cmd[:1] == ["nvidia-smi"]:
            return None
        if cmd[:1] == ["rocminfo"]:
            return _cp("  Marketing Name:    AMD Instinct MI250\n")
        return None

    monkeypatch.setattr(probe, "_run", fake_run)
    monkeypatch.setattr(probe.os.path, "isdir", lambda p: False)
    hw = detect_hardware()
    assert hw.gpu == "amd" and "MI250" in hw.gpu_name


def test_detect_none_when_no_gpu(monkeypatch):
    monkeypatch.setattr(probe, "_run", lambda cmd, timeout=15.0: None)
    monkeypatch.setattr(probe.os.path, "isdir", lambda p: False)
    assert detect_hardware().gpu is None


# -- the runnable script -------------------------------------------------------


def test_main_dry_run_prints_markers(monkeypatch, capsys):
    monkeypatch.setattr(probe, "detect_hardware", lambda: Hardware("nvidia", 12, "A100"))
    installed = []
    monkeypatch.setattr(probe, "_pip_install", lambda spec, python: installed.append(spec) or True)

    rc = main(["--backend", "auto"])  # no --install -> dry run
    out = capsys.readouterr().out
    assert rc == 0
    assert "PHIDLER_DETECT=NVIDIA GPU" in out
    assert "PHIDLER_RECOMMEND=jax" in out
    assert "PHIDLER_INSTALL=skipped" in out
    assert installed == []  # dry run installs nothing


def test_main_install_runs_pip(monkeypatch, capsys):
    monkeypatch.setattr(probe, "detect_hardware", lambda: Hardware("nvidia", 12, "A100"))
    installed = []
    monkeypatch.setattr(probe, "_pip_install", lambda spec, python: installed.append(spec) or True)

    rc = main(["--backend", "auto", "--install"])
    out = capsys.readouterr().out
    assert rc == 0
    assert installed == ["jax[cuda12]"]
    assert "PHIDLER_INSTALL=ok" in out


def test_main_cpu_needs_no_install(monkeypatch, capsys):
    monkeypatch.setattr(probe, "detect_hardware", lambda: Hardware(None))
    monkeypatch.setattr(probe, "_pip_install", lambda spec, python: (_ for _ in ()).throw(AssertionError("should not install")))
    rc = main(["--backend", "auto", "--install"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PHIDLER_RECOMMEND=cpu" in out
    assert "PHIDLER_INSTALL=skipped" in out
