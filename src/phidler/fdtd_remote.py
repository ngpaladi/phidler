"""Run an FDTD simulation on a remote host over SSH.

This is the remote sibling of fdtd_subprocess.py. The local subprocess path
already packages a whole run into a relocatable bundle (a saved .phidler + a
job.json with *basenames*, see fdtd_subprocess.write_bundle) and reads back a
small result.npz. Remote offload is the same bundle, copied to another machine,
run there with `<remote_python> -m phidler.fdtd_subprocess <job.json>`, and the
result copied back. The recorded movie is a single mid-core plane (a few MB), so
the round trip is cheap.

Transport is plain SSH using a host *alias* from the user's ~/.ssh/config:
phidler shells out to ssh/scp/rsync and lets the user's SSH config + agent/keys
handle authentication — it stores no secrets. Key-based auth is assumed;
BatchMode makes a host that would prompt for a password fail fast instead of
hanging the worker thread.

`run_on_remote` returns the exact same (sim_stub, result_stub, elapsed) shape as
run_in_subprocess, so the FDTD window's result handler is unchanged.

All four functions that actually touch the network (_ssh, _ssh_stream, _scp,
_rsync) are deliberately thin so tests can monkeypatch them and exercise the
bundle/command/parse logic without a real SSH hop.
"""

from __future__ import annotations

import collections
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .fdtd_sim import FdtdParams
from .fdtd_subprocess import _ERROR_TAIL_LINES, load_result_npz, parse_progress_line, write_bundle
from .remote_config import RemoteConfig

# A connection prompt (host key, password) would otherwise block forever inside
# the worker thread; BatchMode turns that into a fast, explicit failure.
_SSH_OPTS = ["-o", "BatchMode=yes"]
# Short connect timeout for the interactive probes (check_remote / mktemp), so a
# dead/wrong host reports quickly instead of hanging the dialog.
_CONNECT_TIMEOUT_S = 15
# Source-tree noise that must never reach the remote editable install: a
# platform-specific .venv or stale egg-info would corrupt it.
_RSYNC_EXCLUDES = (".venv", ".git", "__pycache__", "*.egg-info", "build", "dist")

# photonfdtd isn't on PyPI, so deploy installs it from its public GitHub repo by
# default (no local checkout needed on the machine offloading the run). A user
# who wants a specific local/dev photonfdtd overrides this with
# RemoteConfig.local_photonfdtd_dir. https (not ssh) so the remote needs no
# GitHub credentials for the public repo.
PHOTONFDTD_GIT_URL = "git+https://github.com/ngpaladi/photonfdtd.git"


# ---------------------------------------------------------------------------
# Thin transport layer (the only functions that touch the network — mocked in
# tests). Each builds an argv and runs it; callers handle the return code.
# ---------------------------------------------------------------------------

def _ssh(alias: str, remote_cmd: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run one command on the remote host. `remote_cmd` is passed as a single
    argument: ssh re-joins trailing argv and the *remote* shell re-splits it, so
    any command containing spaces/paths must already be a shlex-quoted string."""
    return subprocess.run(
        ["ssh", *_SSH_OPTS, alias, remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _ssh_stream(alias: str, remote_cmd: str, on_line: Callable[[str], None]) -> int:
    """Run a (long, chatty) remote command, forwarding each output line to
    `on_line` as it arrives. Used by deploy_to_remote so pip/build/CUDA output
    shows live. Returns the exit code. stderr is merged into stdout so errors
    interleave in order."""
    proc = subprocess.Popen(
        ["ssh", *_SSH_OPTS, alias, remote_cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    return proc.wait()


def _scp(src: str, dst: str) -> subprocess.CompletedProcess:
    """Copy one file. Either side may be `alias:remote/path`."""
    return subprocess.run(
        ["scp", *_SSH_OPTS, src, dst],
        capture_output=True, text=True,
    )


def _rsync(src: str, dst: str, excludes: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Mirror a directory tree. `-e ssh …` routes rsync over the same SSH
    transport (and BatchMode), so it honours ~/.ssh/config aliases too."""
    cmd = ["rsync", "-az", "-e", "ssh " + " ".join(_SSH_OPTS)]
    for ex in excludes:
        cmd.append(f"--exclude={ex}")
    cmd += [src, dst]
    return subprocess.run(cmd, capture_output=True, text=True)


def _remote_path(path: str) -> str:
    """Quote a path for the remote shell while still letting a leading ``~/``
    expand. Plain ``shlex.quote`` wraps the whole string in single quotes, which
    stops the remote shell expanding ``~`` to the home directory — so a config
    value like ``~/phidler-remote/.venv/bin/python`` would resolve literally and
    not be found. Pass a leading ``~/`` through unquoted and quote only the rest
    (which keeps spaces/specials safe: ``~/'a b'`` still expands the tilde)."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _remote_error(proc: subprocess.CompletedProcess) -> str:
    """A one-line message from a failed remote command — its last stderr line
    (mirrors fdtd_subprocess._child_error), falling back to the exit code."""
    err = (proc.stderr or "").strip()
    last = err.splitlines()[-1] if err else ""
    return last or f"remote command exited with code {proc.returncode}"


def _check(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {_remote_error(proc)}")
    return proc


# ---------------------------------------------------------------------------
# Locating the local source checkouts to deploy
# ---------------------------------------------------------------------------

def _editable_checkout_dir(package: str) -> Path | None:
    """The local source-checkout root of an editable ('pip install -e') install
    of `package`, or None. Reads the canonical direct_url.json
    (PEP 610) first; falls back to walking up from the imported module."""
    import importlib.metadata as md

    try:
        raw = md.distribution(package).read_text("direct_url.json")
    except Exception:
        raw = None
    if raw:
        import json
        from urllib.parse import urlparse, unquote
        try:
            info = json.loads(raw)
            if info.get("dir_info", {}).get("editable") and info.get("url", "").startswith("file:"):
                return Path(unquote(urlparse(info["url"]).path))
        except Exception:
            pass
    # Fallback: import it and assume the <root>/src/<pkg>/__init__.py layout both
    # phidler and photonfdtd use.
    try:
        mod = __import__(package)
        return Path(mod.__file__).resolve().parents[2]
    except Exception:
        return None


def _local_checkouts(cfg: RemoteConfig) -> tuple[Path, Path | None]:
    """(phidler_root, photonfdtd_local) to deploy. phidler is always the local
    editable checkout (the user's working copy). photonfdtd_local is the
    configured override checkout to upload, or None — meaning install photonfdtd
    from GitHub (PHOTONFDTD_GIT_URL) instead of needing a local checkout at all,
    which a machine that only offloads FDTD often won't have."""
    phidler_root = _editable_checkout_dir("phidler")
    if phidler_root is None:
        raise RuntimeError(
            "Can't locate the local phidler source checkout to deploy "
            "(phidler isn't an editable 'pip install -e' install)."
        )
    photonfdtd_local: Path | None = None
    if cfg.local_photonfdtd_dir:
        photonfdtd_local = Path(cfg.local_photonfdtd_dir).expanduser()
        if not photonfdtd_local.exists():
            raise RuntimeError(f"Configured photonfdtd checkout does not exist: {photonfdtd_local}")
    return phidler_root, photonfdtd_local


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_remote(cfg: RemoteConfig) -> tuple[bool, str]:
    """Quick readiness probe: can the remote python import phidler + photonfdtd?
    Returns (ok, message). Never raises — connection/auth errors come back as
    (False, <reason>) so the dialog can show them."""
    if not cfg.is_configured():
        return False, "Set an SSH host first."
    payload = "import phidler, photonfdtd; print('ok')"
    remote_cmd = f"{_remote_path(cfg.resolved_remote_python())} -c {shlex.quote(payload)}"
    try:
        proc = _ssh(cfg.alias, remote_cmd, timeout=_CONNECT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"Timed out connecting to '{cfg.alias}' (no response in {_CONNECT_TIMEOUT_S}s)."
    except Exception as exc:  # ssh missing, etc.
        return False, str(exc)
    if proc.returncode == 0 and "ok" in (proc.stdout or ""):
        return True, f"Connected to '{cfg.alias}': phidler and photonfdtd import successfully."
    return False, _remote_error(proc) or "Remote import check failed."


def run_on_remote(
    document, params: FdtdParams, region_um=None, cfg: RemoteConfig | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[Any, Any, float]:
    """Build the job bundle locally, run it on the remote host, and bring the
    result back — returning ``(sim_stub, result_stub, elapsed)`` identical in
    shape to run_in_subprocess so the caller is none the wiser.
    ``progress_callback(step, n_steps)``, if given, is called as the remote
    solve streams progress markers back over SSH.

    Raises RuntimeError (with the remote's error message) on any transport or
    solve failure."""
    if cfg is None or not cfg.is_configured():
        raise RuntimeError("No remote server is configured (set an SSH host).")

    with tempfile.TemporaryDirectory(prefix="phidler_fdtd_") as tmp_name:
        tmp = Path(tmp_name)
        write_bundle(tmp, document, params, region_um)

        # A fresh per-run dir on the remote (distinct from the code-install dir),
        # so concurrent/successive runs never collide; cleaned in `finally`.
        mk = _check(_ssh(cfg.alias, "mktemp -d -t phidler_fdtd.XXXXXX", timeout=_CONNECT_TIMEOUT_S),
                    "Creating remote work directory")
        remote_dir = mk.stdout.strip()
        if not remote_dir:
            raise RuntimeError("Remote mktemp returned no directory.")

        try:
            _push_bundle(cfg.alias, tmp, remote_dir)

            remote_job = f"{remote_dir}/job.json"  # remote_dir is an absolute mktemp path
            run_cmd = f"{_remote_path(cfg.resolved_remote_python())} -m phidler.fdtd_subprocess {shlex.quote(remote_job)}"
            # Stream the remote run: the deployed child prints the same progress
            # markers to stdout, which arrive here line-by-line over SSH. Parse
            # them for the progress bar; keep other lines as an error tail.
            tail: collections.deque[str] = collections.deque(maxlen=_ERROR_TAIL_LINES)

            def _on_line(line: str) -> None:
                prog = parse_progress_line(line)
                if prog is not None:
                    if progress_callback is not None:
                        progress_callback(prog[0], prog[1])
                elif line.strip():
                    tail.append(line)

            t0 = time.time()
            rc = _ssh_stream(cfg.alias, run_cmd, _on_line)
            elapsed = time.time() - t0
            if rc != 0:
                detail = (tail[-1] if tail else "") or f"exited with code {rc}"
                raise RuntimeError(f"Remote simulation failed: {detail}")

            local_out = tmp / "result.npz"
            pull = _scp(f"{cfg.alias}:{remote_dir}/result.npz", str(local_out))
            if pull.returncode != 0 or not local_out.exists():
                raise RuntimeError(
                    "Remote run produced no result. " + (_remote_error(pull) or _remote_error(proc))
                )

            sim_stub, result_stub = load_result_npz(local_out)
            return sim_stub, result_stub, elapsed
        finally:
            # Best-effort cleanup; don't mask a real failure with a cleanup one.
            try:
                _ssh(cfg.alias, f"rm -rf {shlex.quote(remote_dir)}", timeout=_CONNECT_TIMEOUT_S)
            except Exception:
                pass


def _push_bundle(alias: str, local_tmp: Path, remote_dir: str) -> None:
    """Copy the bundle (job.json + job.phidler) into the remote work dir. Tries
    rsync, falling back to two scps if rsync is unavailable (it's only two small
    files, so either works)."""
    try:
        proc = _rsync(f"{local_tmp}/", f"{alias}:{remote_dir}/")
        if proc.returncode == 0:
            return
        # rsync ran but failed for a non-"missing binary" reason — surface it.
        raise RuntimeError(f"Uploading the job to the remote failed: {_remote_error(proc)}")
    except FileNotFoundError:
        pass  # rsync not installed locally — fall back to scp
    for name in ("job.json", "job.phidler"):
        _check(_scp(str(local_tmp / name), f"{alias}:{remote_dir}/{name}"),
               f"Uploading {name}")


def deploy_to_remote(cfg: RemoteConfig, on_line: Callable[[str], None]) -> bool:
    """One-time setup: rsync the local phidler + photonfdtd source checkouts to
    the remote and `pip install -e` both into the remote Python's environment,
    streaming all output to `on_line`. Returns True on success.

    With just a host configured, this installs under DEFAULT_REMOTE_DIR and
    creates a venv there itself. If the user overrode remote_python, that
    interpreter must already exist (phidler installs *into* it). photonfdtd is
    installed first because it isn't on PyPI, so phidler's [fdtd] extra can't
    pull it."""
    if not cfg.is_configured():
        on_line("Set an SSH host first.")
        return False

    try:
        phidler_root, photonfdtd_local = _local_checkouts(cfg)
    except RuntimeError as exc:
        on_line(str(exc))
        return False

    remote_dir = cfg.resolved_remote_dir()
    on_line(f"Local phidler:    {phidler_root}")
    on_line(
        f"Local photonfdtd: {photonfdtd_local}" if photonfdtd_local
        else f"photonfdtd: from GitHub ({PHOTONFDTD_GIT_URL})"
    )
    on_line(f"Creating remote directory {remote_dir} …")
    mk = _ssh(cfg.alias, f"mkdir -p {_remote_path(remote_dir)}", timeout=_CONNECT_TIMEOUT_S)
    if mk.returncode != 0:
        on_line(_remote_error(mk))
        return False

    rpy = _remote_path(cfg.resolved_remote_python())
    base = _remote_path(remote_dir)  # leading ~ still expands; rest quoted

    # With the managed default interpreter, create the venv if it isn't there
    # yet (so the user never has to set one up by hand). A user-supplied
    # remote_python is assumed to already exist and is left untouched.
    if cfg.uses_managed_venv():
        venv_dir = _remote_path(f"{remote_dir}/.venv")
        on_line("Ensuring a Python venv on the remote …")
        rc = _ssh_stream(cfg.alias, f"test -x {rpy} || python3 -m venv {venv_dir}", on_line)
        if rc != 0:
            on_line("Could not create the remote venv (need python3 with the venv module).")
            return False
        _ssh_stream(cfg.alias, f"{rpy} -m pip install --upgrade pip", on_line)

    # Upload the local checkouts: always phidler, plus photonfdtd only when a
    # local override is set (otherwise it comes from GitHub, nothing to upload).
    uploads = [("phidler", phidler_root)]
    if photonfdtd_local is not None:
        uploads.insert(0, ("photonfdtd", photonfdtd_local))
    for label, root in uploads:
        # rsync the checkout *directory* into remote_dir, yielding
        # remote_dir/<root.name> (no trailing slash on src → copy the dir itself).
        dest = f"{cfg.alias}:{remote_dir}/"
        on_line(f"Uploading {label} source → {remote_dir}/{root.name} …")
        rs = _rsync(str(root).rstrip("/"), dest, _RSYNC_EXCLUDES)
        if rs.returncode != 0:
            on_line(f"Upload failed: {_remote_error(rs)}")
            return False

    # photonfdtd first (not on PyPI) — from GitHub by default, or the uploaded
    # local checkout if overridden — then phidler with its fdtd extra. Install
    # targets under <base>/<name>; the "[fdtd]" extra's brackets are shell glob
    # chars, so quote the target separately to keep it literal.
    if photonfdtd_local is not None:
        pf_cmd = f"{rpy} -m pip install -e {base}/{shlex.quote(photonfdtd_local.name)}"
    else:
        pf_cmd = f"{rpy} -m pip install {shlex.quote(PHOTONFDTD_GIT_URL)}"
    installs = [
        ("photonfdtd", pf_cmd),
        ("phidler", f"{rpy} -m pip install -e {base}/{shlex.quote(phidler_root.name + '[fdtd]')}"),
    ]
    for label, cmd in installs:
        on_line(f"\nInstalling {label} on the remote …")
        rc = _ssh_stream(cfg.alias, cmd, on_line)
        if rc != 0:
            on_line(f"pip install of {label} failed (exit {rc}).")
            return False

    on_line("\nVerifying the remote install …")
    ok, msg = check_remote(cfg)
    on_line(msg)
    return ok
