"""The JAX GPU check: classification of what a fresh JAX process reports."""
import json
import subprocess

import pytest

from phidler import jax_check


def _fake(monkeypatch, info, nvidia, stderr=""):
    monkeypatch.setattr(jax_check, "_nvidia", lambda: nvidia)

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=jax_check._MARKER + json.dumps(info) + "\n",
                                           stderr=stderr)

    monkeypatch.setattr(jax_check.subprocess, "run", run)


BASE = {"jax": "0.10.2", "jaxlib": "0.10.2", "plugins": {}, "backend": "cpu",
        "devices": ["cpu:cpu"], "gpu_compute": None, "gpu_compute_ms": None, "error": None}


def test_gpu_in_use_is_ok(monkeypatch):
    info = {**BASE, "plugins": {"jax-cuda12-plugin": "0.10.2"}, "backend": "gpu",
            "devices": ["gpu:NVIDIA GeForce RTX 4080"], "gpu_compute": True, "gpu_compute_ms": 0.2}
    _fake(monkeypatch, info, ("NVIDIA GeForce RTX 4080", 13))
    r = jax_check.check_jax_gpu()
    assert r.status == "ok" and r.ok and "RTX 4080" in r.summary


def test_no_nvidia_gpu_means_cpu_is_expected(monkeypatch):
    _fake(monkeypatch, BASE, None)
    assert jax_check.check_jax_gpu().status == "no_gpu"


def test_gpu_without_cuda_build_says_what_to_install(monkeypatch):
    _fake(monkeypatch, BASE, ("RTX", 12))
    r = jax_check.check_jax_gpu(python="/venv/bin/python")
    assert r.status == "plugin_missing"
    assert '/venv/bin/python -m pip install "jax[cuda12]==0.10.2"' in r.advice


def test_old_driver_is_reported(monkeypatch):
    _fake(monkeypatch, {**BASE, "plugins": {"jax-cuda12-plugin": "0.10.2"}}, ("RTX", 11))
    assert jax_check.check_jax_gpu().status == "driver_too_old"


def test_plugin_that_fails_to_load_shows_jax_log(monkeypatch):
    log = "W external/xla/xla/service/platform_util.cc:206] Unable to load cuSPARSE. Is it installed?\nunrelated line\n"
    _fake(monkeypatch, {**BASE, "plugins": {"jax-cuda12-plugin": "0.10.2"}}, ("RTX", 12), stderr=log)
    r = jax_check.check_jax_gpu()
    assert r.status == "plugin_failed"
    assert any("cuSPARSE" in ln for ln in r.log) and "unrelated line" not in r.log
    assert "LD_LIBRARY_PATH" in r.advice and "cuSPARSE" in r.text()


def test_missing_jax(monkeypatch):
    _fake(monkeypatch, {**BASE, "jax": None, "error": "ModuleNotFoundError: No module named 'jax'"}, None)
    r = jax_check.check_jax_gpu()
    assert r.status == "jax_missing" and "pip install jax" in r.advice


def test_real_check_runs_against_this_interpreter():
    """No mocks: the check completes and classifies whatever this machine has."""
    r = jax_check.check_jax_gpu()
    assert r.status in ("ok", "no_gpu", "plugin_missing", "driver_too_old", "plugin_failed")
    assert r.jax is not None


def test_fdtd_window_check_button_reports(qapp):
    from PySide6.QtCore import QCoreApplication

    from phidler.main_window import MainWindow
    from phidler.panels.fdtd_window import FdtdWindow

    import time

    win = MainWindow()
    fw = FdtdWindow(win.document, win.view)
    fw.jax_check_button.click()
    assert not fw.jax_check_button.isEnabled()
    end = time.time() + 180
    while time.time() < end and getattr(fw, "_last_jax_report", None) is None:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    assert fw._last_jax_report is not None and fw.jax_check_button.isEnabled()
    assert fw._jax_report_box.detailedText().startswith(fw._last_jax_report.summary)
    fw._jax_report_box.close()
    fw.close()
