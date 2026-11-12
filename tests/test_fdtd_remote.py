"""Remote FDTD offload over SSH. The four functions that actually touch the
network (_ssh / _ssh_stream / _scp / _rsync) are monkeypatched, so these
exercise the bundle/command/parse logic with no real SSH hop."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

import phidler.fdtd_remote as fr
from phidler.fdtd_sim import FdtdParams, SourceSpec
from phidler.model.document import LayoutDocument
from phidler.remote_config import RemoteConfig

REMOTE_DIR = "/remote/tmp/phidler_fdtd.AAAA"


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_fake_npz(path, *, device="CPU"):
    """A stand-in for the result the remote run would produce."""
    np.savez(
        path,
        ez=np.zeros((3, 10, 8), dtype=np.float32),
        x=np.arange(10), y=np.arange(8), z=np.arange(5),
        shape=np.asarray([10, 8, 5]),
        device=device,
    )


class FakeTransport:
    """Records every transport call and fabricates plausible responses."""

    def __init__(self, fail_run=False, gpu=False):
        self.ssh_cmds = []
        self.scp_calls = []
        self.rsync_calls = []
        self.stream_cmds = []
        self.fail_run = fail_run
        self.gpu = gpu

    def ssh(self, alias, remote_cmd, timeout=None):
        self.ssh_cmds.append(remote_cmd)
        if "mktemp" in remote_cmd:
            return _cp(0, stdout=REMOTE_DIR + "\n")
        if "fdtd_subprocess" in remote_cmd:
            return _cp(1, stderr="Traceback\nRuntimeError: boom\n") if self.fail_run else _cp(0)
        # A typical server: bare `python3` is too old for gdsfactory, but a
        # versioned python3.12 is available for the venv.
        if remote_cmd.startswith("command -v "):
            wanted = remote_cmd.split()[-1].strip("'\"")
            return _cp(0, stdout=f"/usr/bin/{wanted}\n") if wanted == "python3.12" else _cp(1)
        if "sys.version_info" in remote_cmd:
            if ".venv/bin/python" in remote_cmd:
                return _cp(1)  # the managed venv doesn't exist yet
            return _cp(0, stdout="3.10\n")  # distro python3 too old
        return _cp(0, stdout="ok\n")  # mkdir, rm -rf, import-check

    def scp(self, src, dst):
        self.scp_calls.append((src, dst))
        if ":" in src and str(dst).endswith("result.npz"):  # the pull-back
            _write_fake_npz(dst, device="GPU" if self.gpu else "CPU")
        return _cp(0)

    def rsync(self, src, dst, excludes=()):
        self.rsync_calls.append((src, dst, excludes))
        return _cp(0)

    def stream(self, alias, remote_cmd, on_line):
        self.stream_cmds.append(remote_cmd)
        if "fdtd_subprocess" in remote_cmd:  # the streamed solve emits progress markers
            on_line("@@PHIDLER_PROGRESS 0 10")
            on_line("@@PHIDLER_PROGRESS 5 10")
            on_line("@@PHIDLER_PROGRESS 10 10")
            if self.fail_run:
                on_line("Traceback")
                on_line("RuntimeError: boom")
                return 1
            return 0
        on_line(f"$ {remote_cmd}")
        return 0


def _install(monkeypatch, t):
    monkeypatch.setattr(fr, "_ssh", t.ssh)
    monkeypatch.setattr(fr, "_scp", t.scp)
    monkeypatch.setattr(fr, "_rsync", t.rsync)
    monkeypatch.setattr(fr, "_ssh_stream", t.stream)


def _cfg(**kw):
    base = dict(alias="gpubox", remote_dir="~/phidler-remote",
                remote_python="/venv/bin/python", use_gpu=False)
    base.update(kw)
    return RemoteConfig(**base)


def _doc():
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 8.0, "width": 0.5})
    return doc


# -- run_on_remote ----------------------------------------------------------

def test_run_on_remote_builds_commands_and_parses_result(qapp, monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    params = FdtdParams(cell_size_um=0.1, sources=(SourceSpec(x_um=-5.0, y_um=0.0),))

    sim_stub, result_stub, elapsed = fr.run_on_remote(_doc(), params, None, _cfg())

    # parsed into exactly the stub contract the display reads
    assert sim_stub.grid.shape == (10, 8, 5)
    assert result_stub.fields["field"]["Ez"].shape == (3, 10, 8)
    assert sim_stub.device == "CPU"
    assert elapsed >= 0

    # the run command invokes the remote python's module entry on the remote job
    # (streamed, so it lands in stream_cmds — that's how progress comes back)
    run_cmd = next(c for c in t.stream_cmds if "fdtd_subprocess" in c)
    assert "/venv/bin/python -m phidler.fdtd_subprocess" in run_cmd
    assert f"{REMOTE_DIR}/job.json" in run_cmd
    # bundle pushed into the per-run dir, result pulled back
    assert t.rsync_calls and t.rsync_calls[0][1] == f"gpubox:{REMOTE_DIR}/"
    assert any(src == f"gpubox:{REMOTE_DIR}/result.npz" for src, _ in t.scp_calls)


def test_run_on_remote_cleans_up_remote_dir(qapp, monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    fr.run_on_remote(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg())
    assert any(f"rm -rf" in c and REMOTE_DIR in c for c in t.ssh_cmds)


def test_run_on_remote_cleanup_fires_even_on_failure(qapp, monkeypatch):
    t = FakeTransport(fail_run=True)
    _install(monkeypatch, t)
    with pytest.raises(RuntimeError) as exc:
        fr.run_on_remote(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg())
    assert "boom" in str(exc.value)  # the remote stderr tail surfaced
    assert any("rm -rf" in c and REMOTE_DIR in c for c in t.ssh_cmds)  # finally still ran


def test_run_on_remote_uses_remote_device_from_result(qapp, monkeypatch):
    """The displayed device comes from where JAX *actually* ran on the remote
    (the returned npz), not what was requested."""
    t = FakeTransport(gpu=True)
    _install(monkeypatch, t)
    sim_stub, _, _ = fr.run_on_remote(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg(backend="jax"))
    assert sim_stub.device == "GPU"
    # ...and a GPU request on a box whose JAX fell back to the CPU says so.
    t = FakeTransport(gpu=False)
    _install(monkeypatch, t)
    sim_stub, _, _ = fr.run_on_remote(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg(backend="jax"))
    assert sim_stub.device == "CPU"


def test_run_on_remote_forwards_progress(qapp, monkeypatch):
    """Progress markers streamed back from the remote solve reach the callback."""
    t = FakeTransport()
    _install(monkeypatch, t)
    ticks = []
    fr.run_on_remote(_doc(), FdtdParams(cell_size_um=0.1), None, _cfg(),
                     progress_callback=lambda i, n: ticks.append((i, n)))
    assert ticks == [(0, 10), (5, 10), (10, 10)]  # markers parsed, not treated as errors


def test_run_on_remote_unconfigured_raises(qapp):
    with pytest.raises(RuntimeError):
        fr.run_on_remote(_doc(), FdtdParams(), None, RemoteConfig())


# -- check_remote -----------------------------------------------------------

def test_check_remote_ok(monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    ok, msg = fr.check_remote(_cfg())
    assert ok is True
    assert "gpubox" in msg
    assert any("import phidler, photonfdtd" in c for c in t.ssh_cmds)


def test_check_remote_failure(monkeypatch):
    def ssh(alias, remote_cmd, timeout=None):
        return _cp(1, stderr="ssh: Could not resolve hostname gpubox\n")
    monkeypatch.setattr(fr, "_ssh", ssh)
    ok, msg = fr.check_remote(_cfg())
    assert ok is False
    assert "resolve hostname" in msg


def test_check_remote_not_configured_does_not_connect(monkeypatch):
    called = []
    monkeypatch.setattr(fr, "_ssh", lambda *a, **k: called.append(a) or _cp(0))
    ok, msg = fr.check_remote(RemoteConfig())
    assert ok is False
    assert not called  # never attempted a connection


# -- deploy_to_remote -------------------------------------------------------

def test_deploy_installs_photonfdtd_before_phidler(monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(
        fr, "_local_checkouts",
        lambda cfg: (Path("/local/phidler"), Path("/local/photonfdtd")),
    )
    lines = []
    ok = fr.deploy_to_remote(_cfg(), lines.append)

    assert ok is True
    # both source trees uploaded
    assert {Path(c[0]).name for c in t.rsync_calls} == {"phidler", "photonfdtd"}
    # photonfdtd installed first (not on PyPI), phidler second with its fdtd extra
    assert "photonfdtd" in t.stream_cmds[0] and "pip install -e" in t.stream_cmds[0]
    assert "phidler" in t.stream_cmds[1] and "[fdtd]" in t.stream_cmds[1]


def test_deploy_reports_pip_failure(monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_local_checkouts",
                        lambda cfg: (Path("/local/phidler"), Path("/local/photonfdtd")))
    monkeypatch.setattr(fr, "_ssh_stream", lambda a, c, cb: (cb("error"), 1)[1])  # nonzero exit
    lines = []
    assert fr.deploy_to_remote(_cfg(), lines.append) is False


def test_deploy_creates_a_venv_for_a_bare_host(monkeypatch):
    """With just a host (no remote_python override), setup must create the venv
    itself under the default dir — the whole point of 'just put in a server'."""
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_local_checkouts",
                        lambda cfg: (Path("/local/phidler"), Path("/local/photonfdtd")))
    ok = fr.deploy_to_remote(RemoteConfig(alias="gpubox"), [].append)

    assert ok is True
    # built the venv with a gdsfactory-compatible python (3.12), not the old python3
    venv_cmds = [c for c in t.stream_cmds if "-m venv" in c]
    assert len(venv_cmds) == 1
    assert "python3.12 -m venv" in venv_cmds[0] and "~/phidler-remote/.venv" in venv_cmds[0]
    # installs use the created venv's interpreter
    assert any("phidler-remote/.venv/bin/python -m pip install -e" in c for c in t.stream_cmds)


def test_deploy_with_explicit_python_does_not_create_a_venv(monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_local_checkouts",
                        lambda cfg: (Path("/local/phidler"), Path("/local/photonfdtd")))
    ok = fr.deploy_to_remote(_cfg(), [].append)  # _cfg supplies an explicit remote_python

    assert ok is True
    assert not any("python3 -m venv" in c for c in t.stream_cmds)  # left the user's env alone


def test_deploy_pulls_photonfdtd_from_github_by_default(monkeypatch):
    """With no local photonfdtd override, setup installs photonfdtd from its
    GitHub URL and uploads only the phidler checkout (nothing to rsync for a
    pip-from-git dependency)."""
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_local_checkouts", lambda cfg: (Path("/local/phidler"), None))
    ok = fr.deploy_to_remote(_cfg(), [].append)

    assert ok is True
    # only phidler uploaded — photonfdtd comes from git, not rsync
    assert {Path(c[0]).name for c in t.rsync_calls} == {"phidler"}
    pf_install = next(c for c in t.stream_cmds if fr.PHOTONFDTD_GIT_URL in c)
    assert "pip install" in pf_install and "-e" not in pf_install.split(fr.PHOTONFDTD_GIT_URL)[0]
    assert any("[fdtd]" in c for c in t.stream_cmds)  # phidler still installed with its extra


def test_local_checkouts_defaults_photonfdtd_to_github(qapp):
    """Without an override, photonfdtd is None (→ install from GitHub); phidler
    is still located from its editable install."""
    phidler_root, photonfdtd_local = fr._local_checkouts(RemoteConfig(alias="h"))
    assert phidler_root is not None
    assert photonfdtd_local is None


def test_local_checkouts_honours_a_local_override(qapp, tmp_path):
    _phidler, photonfdtd_local = fr._local_checkouts(
        RemoteConfig(alias="h", local_photonfdtd_dir=str(tmp_path))
    )
    assert photonfdtd_local == tmp_path

    with pytest.raises(RuntimeError, match="does not exist"):
        fr._local_checkouts(RemoteConfig(alias="h", local_photonfdtd_dir="/no/such/photonfdtd"))


def test_deploy_unconfigured_returns_false():
    lines = []
    assert fr.deploy_to_remote(RemoteConfig(), lines.append) is False
    assert lines  # told the user why


# -- path quoting (the ~ expansion footgun) ---------------------------------

def test_remote_path_lets_leading_tilde_expand():
    # plain shlex.quote would wrap the whole thing in quotes and stop ~ expanding
    assert fr._remote_path("~/phidler/.venv/bin/python") == "~/phidler/.venv/bin/python"
    assert fr._remote_path("/opt/venv/bin/python") == "/opt/venv/bin/python"
    assert fr._remote_path("~") == "~"
    # a space after the tilde stays safe, tilde still outside the quotes
    assert fr._remote_path("~/a b").startswith("~/")


def test_check_remote_command_keeps_tilde_unquoted(monkeypatch):
    captured = []
    monkeypatch.setattr(fr, "_ssh", lambda alias, cmd, timeout=None: captured.append(cmd) or _cp(0, "ok\n"))
    fr.check_remote(_cfg(remote_python="~/phidler/.venv/bin/python"))
    assert captured[0].startswith("~/phidler/.venv/bin/python -c ")  # not '~/...'


def test_deploy_commands_keep_tilde_and_quote_extra(monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_local_checkouts",
                        lambda cfg: (Path("/local/phidler"), Path("/local/photonfdtd")))
    fr.deploy_to_remote(_cfg(remote_dir="~/phidler-remote", remote_python="~/phidler-remote/.venv/bin/python"), lambda _l: None)
    phidler_cmd = t.stream_cmds[1]
    assert "~/phidler-remote/" in phidler_cmd          # tilde dir expands
    assert "'phidler[fdtd]'" in phidler_cmd            # the glob-y extra stays literal


# -- FdtdWorker dispatch (locks in the remote branch) -------------------------

def test_fdtd_worker_takes_remote_branch(qapp, monkeypatch):
    """With remote=True the worker must call run_on_remote — not the local
    subprocess, and not an in-process local solve."""
    from phidler.panels.fdtd_window import FdtdWorker

    calls = []
    monkeypatch.setattr(
        fr, "run_on_remote",
        lambda document, params, region_um, cfg, progress_callback=None: (
            calls.append((params, region_um, cfg)) or ("SIM", "RES", 1.5)
        ),
    )
    import phidler.fdtd_subprocess as sub
    monkeypatch.setattr(sub, "run_in_subprocess",
                        lambda *a, **k: pytest.fail("took the local subprocess path, not remote"))
    import phidler.panels.fdtd_window as fw
    monkeypatch.setattr(fw, "build_simulation",
                        lambda *a, **k: pytest.fail("took the local in-process path, not remote"))

    cfg = _cfg(backend="jax")
    worker = FdtdWorker(_doc(), FdtdParams(), region_um=(1, 2, 3, 4), remote=True, remote_cfg=cfg)
    got = []
    worker.finished.connect(lambda s, r, e: got.append((s, r, e)))
    worker.run()

    assert calls and calls[0][2] is cfg  # run_on_remote received the config
    assert got == [("SIM", "RES", 1.5)]  # its result was emitted unchanged


# -- RemoteConfigDialog threading (the lifecycle the unit tests above can't see)

def test_remote_config_dialog_op_completes_without_deadlock(qapp, monkeypatch):
    """Drive the dialog's real QThread with a fast fake check_remote: it must
    finish, clean up its thread, and re-enable the buttons. A deadlock (waiting
    on the thread before its event loop quits) would leave _op_thread set."""
    import time

    monkeypatch.setattr(fr, "check_remote", lambda cfg: (False, "no such host"))
    from phidler.panels.fdtd_window import RemoteConfigDialog

    dlg = RemoteConfigDialog()
    dlg.alias_edit.setText("bogus")
    dlg.remote_python_edit.setText("/x/python")
    dlg._start_op("check")

    for _ in range(300):  # up to ~3s
        qapp.processEvents()
        if dlg._op_thread is None:
            break
        time.sleep(0.01)

    assert dlg._op_thread is None  # thread finished and was cleaned up (no deadlock)
    assert dlg.setup_button.isEnabled() and dlg._buttons.isEnabled()  # buttons restored
    assert "no such host" in dlg.log.toPlainText()


def test_remote_dialog_has_a_backend_selector(qapp):
    from phidler.panels.fdtd_window import RemoteConfigDialog

    dlg = RemoteConfigDialog()
    values = [dlg.backend_combo.itemData(i) for i in range(dlg.backend_combo.count())]
    assert values == ["auto", "cpu", "jax"]  # CuPy is gone with photonfdtd 0.13

    dlg.alias_edit.setText("gpubox")
    dlg.backend_combo.setCurrentIndex(values.index("jax"))
    cfg = dlg._current_config()
    assert cfg.backend == "jax"
    assert cfg.use_gpu is True  # legacy bool kept in sync for older readers
    dlg.backend_combo.setCurrentIndex(values.index("cpu"))
    cfg = dlg._current_config()
    assert cfg.backend == "cpu" and cfg.use_gpu is False


def test_remote_dialog_shows_a_legacy_cupy_config_as_gpu(qapp, monkeypatch):
    """A config saved with backend="cupy" (removed) opens on the JAX GPU entry."""
    import phidler.remote_config as rc
    from phidler.panels.fdtd_window import RemoteConfigDialog

    # The dialog imports load_remote_config from the module at construction.
    monkeypatch.setattr(rc, "load_remote_config", lambda *a, **k: RemoteConfig(alias="h", backend="cupy"))
    dlg = RemoteConfigDialog()
    assert dlg.backend_combo.currentData() == "jax"


def test_smart_setup_skips_install_when_already_ready(qapp, monkeypatch):
    """When the remote already imports phidler+photonfdtd, 'Connect & set up'
    must NOT redeploy, and it saves the config on success."""
    import time

    monkeypatch.setattr(fr, "check_remote", lambda cfg: (True, "ok, ready"))
    deployed = []
    monkeypatch.setattr(fr, "deploy_to_remote", lambda cfg, on_line: deployed.append(True) or True)
    import phidler.remote_config as rc

    saved = []
    monkeypatch.setattr(rc, "save_remote_config", lambda cfg, *a, **k: saved.append(cfg))

    from phidler.panels.fdtd_window import RemoteConfigDialog

    dlg = RemoteConfigDialog()
    dlg.alias_edit.setText("gpubox")
    dlg._start_op("setup")
    for _ in range(300):
        qapp.processEvents()
        if dlg._op_thread is None:
            break
        time.sleep(0.01)

    assert dlg._op_thread is None
    assert deployed == []  # already ready -> no install
    assert len(saved) == 1 and saved[0].alias == "gpubox"  # auto-saved
    assert "already set up" in dlg.log.toPlainText()


def test_smart_setup_installs_when_not_ready(qapp, monkeypatch):
    import time

    # Not ready on the first probe, ready after the (fake) install.
    results = iter([(False, "cannot import phidler"), (True, "ok now")])
    monkeypatch.setattr(fr, "check_remote", lambda cfg: next(results))
    deployed = []
    monkeypatch.setattr(fr, "deploy_to_remote", lambda cfg, on_line: deployed.append(True) or True)
    import phidler.remote_config as rc

    monkeypatch.setattr(rc, "save_remote_config", lambda cfg, *a, **k: None)

    from phidler.panels.fdtd_window import RemoteConfigDialog

    dlg = RemoteConfigDialog()
    dlg.alias_edit.setText("gpubox")
    dlg._start_op("setup")
    for _ in range(300):
        qapp.processEvents()
        if dlg._op_thread is None:
            break
        time.sleep(0.01)

    assert deployed == [True]  # installed because the first probe failed
    assert "Remote is ready" in dlg.log.toPlainText()


def test_auto_setup_adopts_the_probe_recommendation(qapp, monkeypatch):
    """With Auto selected, the remote hardware probe's recommendation (streamed as
    PHIDLER_RECOMMEND=…) is adopted as the concrete backend and saved."""
    import time

    # Not ready first; ready after the fake install. The fake deploy emits the
    # probe's recommendation line, exactly as the real one streams it.
    results = iter([(False, "not ready"), (True, "ready")])
    monkeypatch.setattr(fr, "check_remote", lambda cfg: next(results))

    def fake_deploy(cfg, on_line):
        on_line("Detecting remote hardware …")
        on_line("PHIDLER_RECOMMEND=jax")
        return True

    monkeypatch.setattr(fr, "deploy_to_remote", fake_deploy)
    import phidler.remote_config as rc

    saved = []
    monkeypatch.setattr(rc, "save_remote_config", lambda cfg, *a, **k: saved.append(cfg))

    from phidler.panels.fdtd_window import RemoteConfigDialog

    dlg = RemoteConfigDialog()
    dlg.alias_edit.setText("gpubox")
    # Ensure Auto is selected.
    dlg.backend_combo.setCurrentIndex([dlg.backend_combo.itemData(i) for i in range(dlg.backend_combo.count())].index("auto"))
    dlg._start_op("setup")
    for _ in range(300):
        qapp.processEvents()
        if dlg._op_thread is None:
            break
        time.sleep(0.01)

    assert dlg._detected_backend == "jax"
    assert dlg.backend_combo.currentData() == "jax"  # combo adopted the concrete backend
    assert saved and saved[-1].backend == "jax"  # saved as concrete, not "auto"


def test_deploy_runs_the_hardware_probe(monkeypatch, tmp_path):
    """deploy_to_remote invokes phidler.remote_setup_probe on the remote to set up
    the ideal accelerator."""
    cfg = RemoteConfig(alias="h", remote_python="/x/py", backend="auto")

    # Make the upfront steps succeed cheaply.
    monkeypatch.setattr(fr, "_local_checkouts", lambda c: (Path("/local/phidler"), None))
    monkeypatch.setattr(fr, "_ssh", lambda alias, cmd, timeout=None: _cp(0, "ok"))
    monkeypatch.setattr(fr, "_upload_tree", lambda *a, **k: True)
    monkeypatch.setattr(fr, "check_remote", lambda c: (True, "ok"))

    commands = []

    def fake_stream(alias, cmd, on_line):
        commands.append(cmd)
        return 0

    monkeypatch.setattr(fr, "_ssh_stream", fake_stream)

    ok = fr.deploy_to_remote(cfg, lambda line: None)
    assert ok
    assert any("remote_setup_probe" in c and "--backend auto" in c and "--install" in c for c in commands)


def test_summarize_remote_backends_parses_the_probe_line():
    out = "ok\nbackends numba=True jax=True jax_gpu=False cupy=False\n"
    summary = fr._summarize_remote_backends(out)
    assert "CPU/Numba: available" in summary
    assert "GPU (JAX): CPU-only" in summary  # jax present but no GPU device
    assert "GPU (CuPy, legacy): not installed" in summary
    assert fr._summarize_remote_backends("no marker here") == ""


def test_remote_config_dialog_refuses_to_close_mid_operation(qapp):
    """Closing the dialog while an op runs would destroy a live QThread and
    abort the process — reject() must be a no-op until it's done."""
    from PySide6.QtGui import QCloseEvent

    from phidler.panels.fdtd_window import RemoteConfigDialog

    dlg = RemoteConfigDialog()
    rejected = []
    dlg.rejected.connect(lambda: rejected.append(True))

    dlg._op_thread = object()  # pretend an operation is in flight
    dlg.reject()
    qapp.processEvents()
    assert not rejected  # guarded — did not actually reject/tear down

    event = QCloseEvent()
    dlg.closeEvent(event)
    assert not event.isAccepted()  # window-manager close blocked too

    dlg._op_thread = None  # now it rejects normally
    dlg.reject()
    qapp.processEvents()
    assert rejected


# -- resilience to an unclean remote shell / missing rsync ------------------

def test_upload_tree_falls_back_to_tar_when_rsync_fails(monkeypatch):
    monkeypatch.setattr(fr, "_rsync", lambda *a, **k: _cp(23, stderr="protocol version mismatch"))
    tar_args = {}
    monkeypatch.setattr(fr, "_tar_upload_dir",
                        lambda alias, root, rd, ex: tar_args.update(alias=alias, root=root, rd=rd) or (0, "ok"))
    lines = []
    ok = fr._upload_tree("gpubox", Path("/local/phidler"), "~/phidler-remote", (".venv",), lines.append)
    assert ok is True
    assert tar_args == {"alias": "gpubox", "root": Path("/local/phidler"), "rd": "~/phidler-remote"}
    assert any("tar over ssh" in ln for ln in lines)


def test_upload_tree_falls_back_to_tar_when_rsync_missing(monkeypatch):
    def _missing(*a, **k):
        raise FileNotFoundError("rsync")
    monkeypatch.setattr(fr, "_rsync", _missing)
    monkeypatch.setattr(fr, "_tar_upload_dir", lambda *a: (0, ""))
    assert fr._upload_tree("h", Path("/x"), "~/d", (), lambda s: None) is True


def test_upload_tree_reports_when_tar_also_fails(monkeypatch):
    monkeypatch.setattr(fr, "_rsync", lambda *a, **k: _cp(1))
    monkeypatch.setattr(fr, "_tar_upload_dir", lambda *a: (2, "tar: broken pipe"))
    lines = []
    assert fr._upload_tree("h", Path("/x"), "~/d", (), lines.append) is False
    assert any("Upload failed" in ln for ln in lines)


def test_deploy_uses_tar_fallback_when_rsync_is_unclean(monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_rsync", lambda *a, **k: _cp(23, stderr="is your shell clean?"))
    tar_uploads = []
    monkeypatch.setattr(fr, "_tar_upload_dir",
                        lambda alias, root, rd, ex: tar_uploads.append(root.name) or (0, ""))
    monkeypatch.setattr(fr, "_local_checkouts", lambda cfg: (Path("/local/phidler"), None))
    assert fr.deploy_to_remote(_cfg(), [].append) is True
    assert tar_uploads == ["phidler"]  # uploaded via the tar fallback, not rsync


def test_run_pushes_bundle_via_scp_when_rsync_is_unclean(qapp, monkeypatch):
    t = FakeTransport()
    _install(monkeypatch, t)
    monkeypatch.setattr(fr, "_rsync", lambda *a, **k: _cp(23, stderr="protocol mismatch"))
    fr.run_on_remote(_doc(), FdtdParams(), None, _cfg())
    pushed = [dst for (_src, dst) in t.scp_calls if str(dst).endswith(("job.json", "job.phidler"))]
    assert len(pushed) == 2  # both bundle files fell back to scp, no RuntimeError


def test_run_survives_a_chatty_login_shell_banner(qapp, monkeypatch):
    t = FakeTransport()
    orig = t.ssh

    def noisy(alias, cmd, timeout=None):
        if "mktemp" in cmd:  # a banner/MOTD before mktemp's actual output
            return _cp(0, stdout=f"Welcome to gpubox!\nLast login: today\n{REMOTE_DIR}\n")
        return orig(alias, cmd, timeout)

    monkeypatch.setattr(fr, "_ssh", noisy)
    monkeypatch.setattr(fr, "_scp", t.scp)
    monkeypatch.setattr(fr, "_rsync", t.rsync)
    monkeypatch.setattr(fr, "_ssh_stream", t.stream)
    fr.run_on_remote(_doc(), FdtdParams(), None, _cfg())
    run_cmd = next(c for c in t.stream_cmds if "fdtd_subprocess" in c)
    assert REMOTE_DIR in run_cmd  # used the real dir, not the banner text


# -- gdsfactory-compatible remote Python selection --------------------------

def test_pick_remote_python_prefers_a_versioned_interpreter(monkeypatch):
    def ssh(alias, cmd, timeout=None):
        if cmd.startswith("command -v"):
            return _cp(0, "/usr/bin/python3.12\n") if "python3.12" in cmd else _cp(1)
        return _cp(0, "3.10\n")  # bare python3 is too old
    monkeypatch.setattr(fr, "_ssh", ssh)
    assert fr._pick_remote_python("h", lambda s: None) == "python3.12"


def test_pick_remote_python_uses_python3_when_in_range(monkeypatch):
    def ssh(alias, cmd, timeout=None):
        if cmd.startswith("command -v"):
            return _cp(1)  # no versioned names present
        return _cp(0, "3.11\n")  # but python3 itself is 3.11
    monkeypatch.setattr(fr, "_ssh", ssh)
    assert fr._pick_remote_python("h", lambda s: None) == "python3"


def test_pick_remote_python_reports_when_none_is_new_enough(monkeypatch):
    def ssh(alias, cmd, timeout=None):
        if cmd.startswith("command -v"):
            return _cp(1)
        return _cp(0, "3.10\n")  # only an out-of-range python3
    monkeypatch.setattr(fr, "_ssh", ssh)
    lines = []
    assert fr._pick_remote_python("h", lines.append) is None
    assert any("3.11" in ln and "3.13" in ln for ln in lines)  # message states the required range


def test_ensure_remote_venv_reuses_a_compatible_existing_venv(monkeypatch):
    def ssh(alias, cmd, timeout=None):
        if "sys.version_info" in cmd and ".venv/bin/python" in cmd:
            return _cp(0, "3.12\n")  # an existing venv on a supported python
        return _cp(0, "")
    monkeypatch.setattr(fr, "_ssh", ssh)
    streamed = []
    monkeypatch.setattr(fr, "_ssh_stream", lambda a, c, cb: streamed.append(c) or 0)
    assert fr._ensure_remote_venv("h", "~/d/.venv", lambda s: None) is True
    assert not any("venv" in c for c in streamed)  # nothing rebuilt


def test_ensure_remote_venv_fails_clearly_when_python_too_old(monkeypatch):
    def ssh(alias, cmd, timeout=None):
        if cmd.startswith("command -v"):
            return _cp(1)  # no versioned python
        if "sys.version_info" in cmd:
            return _cp(1) if ".venv/bin/python" in cmd else _cp(0, "3.10\n")
        return _cp(0, "")
    monkeypatch.setattr(fr, "_ssh", ssh)
    monkeypatch.setattr(fr, "_ssh_stream", lambda a, c, cb: 0)
    lines = []
    assert fr._ensure_remote_venv("h", "~/d/.venv", lines.append) is False
    assert any("gdsfactory needs 3.11" in ln for ln in lines)


def test_sourced_loads_bashrc_and_suppresses_its_output():
    wrapped = fr._sourced("command -v python3.12")
    assert ". ~/.bashrc" in wrapped           # loads the user's env first
    assert ">/dev/null 2>&1" in wrapped        # but hides its output (parse-safe)
    assert wrapped.endswith("; command -v python3.12")


# -- Picking the server from ~/.ssh/config

@pytest.fixture
def ssh_hosts_env(tmp_path, monkeypatch):
    """Three hosts in a fake ssh config, and a throwaway per-user settings store."""
    from PySide6.QtCore import QSettings

    from phidler import remote_config, ssh_hosts

    store = QSettings(str(tmp_path / "remote.ini"), QSettings.IniFormat)
    monkeypatch.setattr(remote_config, "_settings", lambda settings=None: store if settings is None else settings)
    monkeypatch.setattr(ssh_hosts, "list_ssh_hosts", lambda *a: ["gpubox", "cluster.edu", "lab"])
    return store


def test_remote_dialog_offers_ssh_config_hosts_with_their_settings(qapp, ssh_hosts_env):
    from phidler.panels.fdtd_window import RemoteConfigDialog
    from phidler.remote_config import RemoteConfig, save_remote_config

    save_remote_config(RemoteConfig(alias="lab", backend="jax", remote_dir="/data/ph"))
    save_remote_config(RemoteConfig(alias="gpubox", backend="cpu"))

    dlg = RemoteConfigDialog()
    combo = dlg.host_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["gpubox", "cluster.edu", "lab"]
    assert dlg.alias_edit.text() == "gpubox"

    combo.setCurrentIndex(2)
    combo.activated.emit(2)  # what a pick from the list does
    cfg = dlg._current_config()
    assert (cfg.alias, cfg.backend, cfg.remote_dir) == ("lab", "jax", "/data/ph")

    combo.setCurrentIndex(1)
    combo.activated.emit(1)  # never configured: defaults, not the last host's
    assert dlg._current_config() == RemoteConfig(alias="cluster.edu", backend="auto")

    dlg.alias_edit.setText("typed-host")  # a host not in the config still works
    assert dlg._current_config().alias == "typed-host"


def test_run_row_selects_the_remote_server(qapp, ssh_hosts_env):
    from phidler.main_window import MainWindow
    from phidler.panels.fdtd_window import FdtdWindow
    from phidler.remote_config import RemoteConfig, load_remote_config, save_remote_config

    save_remote_config(RemoteConfig(alias="oldbox", backend="jax"))  # not in the config
    win = MainWindow()
    fw = FdtdWindow(win.document, win.view)
    combo = fw.remote_host_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["oldbox", "gpubox", "cluster.edu", "lab"]
    assert combo.currentText() == "oldbox"

    combo.setCurrentIndex(3)
    combo.activated.emit(3)
    assert load_remote_config() == RemoteConfig(alias="lab")

    combo.setCurrentIndex(0)
    combo.activated.emit(0)
    assert load_remote_config().backend == "jax"  # its saved settings came back


def test_settings_saved_before_per_host_storage_survive_a_switch(qapp, ssh_hosts_env):
    """A config written by an older version has only the flat keys. Switching to
    another host and back, or re-picking the host in the dialog, must keep it."""
    from phidler.main_window import MainWindow
    from phidler.panels.fdtd_window import FdtdWindow, RemoteConfigDialog
    from phidler.remote_config import load_remote_config

    for key, value in (("alias", "gpubox"), ("backend", "jax"), ("remote_dir", "/data/ph")):
        ssh_hosts_env.setValue(f"remote/{key}", value)

    dlg = RemoteConfigDialog()
    dlg.host_combo.setCurrentIndex(0)
    dlg.host_combo.activated.emit(0)  # re-pick the current host
    cfg = dlg._current_config()
    assert (cfg.alias, cfg.backend, cfg.remote_dir) == ("gpubox", "jax", "/data/ph")

    win = MainWindow()
    combo = FdtdWindow(win.document, win.view).remote_host_combo
    for index in (2, 0):  # away to "lab", then back
        combo.setCurrentIndex(index)
        combo.activated.emit(index)
    back = load_remote_config()
    assert (back.alias, back.backend, back.remote_dir) == ("gpubox", "jax", "/data/ph")


def test_run_on_a_host_that_is_not_set_up_says_so(monkeypatch):
    def fake_ssh(alias, cmd, timeout=None):
        return subprocess.CompletedProcess([], 0, stdout="/tmp/phidler_fdtd.x\n", stderr="")

    def fake_stream(alias, cmd, on_line):
        on_line("bash: line 1: /home/me/phidler-remote/.venv/bin/python: No such file or directory")
        return 127

    monkeypatch.setattr(fr, "_ssh", fake_ssh)
    monkeypatch.setattr(fr, "_ssh_stream", fake_stream)
    monkeypatch.setattr(fr, "_push_bundle", lambda *a: None)
    monkeypatch.setattr(fr, "write_bundle", lambda *a: None)
    with pytest.raises(RuntimeError, match="doesn't look set up.*Connect && set up"):
        fr.run_on_remote(object(), object(), cfg=RemoteConfig(alias="lab"))
