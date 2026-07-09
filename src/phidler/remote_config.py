"""Per-user configuration for offloading FDTD runs to a remote SSH host.

Backed by QSettings (per-user, survives restarts), the same mechanism
recent_projects.py uses. A remote server is a property of the user's
*environment*, not of any one photonic *design*, so this is deliberately kept
out of the .phidler project file / SimulationConfig.

The functions take an optional QSettings so tests can pass a throwaway one
instead of touching the real user settings.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from PySide6.QtCore import QSettings

_ORG, _APP = "phidler", "phidler"
_PREFIX = "remote"

# Default install location on the remote when the user doesn't override it, so
# all they have to enter is a host. Deploy creates a venv under here and phidler
# runs out of it, so remote_dir + remote_python are both derived from this.
DEFAULT_REMOTE_DIR = "~/phidler-remote"


@dataclass
class RemoteConfig:
    """Where and how to run an offloaded FDTD job.

    `alias` is a host alias from the user's ~/.ssh/config (phidler shells out to
    `ssh <alias> …` and lets SSH config + agent/keys handle auth — it stores no
    secrets) and is the *only* required field. `remote_dir` is where phidler
    rsyncs the phidler + photonfdtd source checkouts and installs them; blank
    means DEFAULT_REMOTE_DIR. `remote_python` is the interpreter runs invoke
    (`<remote_python> -m phidler.fdtd_subprocess`); blank means a managed venv
    under `remote_dir` that deploy creates. `use_gpu` requests the remote GPU
    backend independently of the local machine's GPU. `local_photonfdtd_dir`
    overrides where deploy finds the local photonfdtd checkout to rsync — needed
    because a user offloading FDTD may have no importable local photonfdtd."""

    alias: str = ""
    remote_dir: str = ""
    remote_python: str = ""
    use_gpu: bool = False
    local_photonfdtd_dir: str = ""

    def resolved_remote_dir(self) -> str:
        """The install dir to use — the override, or the default under $HOME."""
        return self.remote_dir or DEFAULT_REMOTE_DIR

    def resolved_remote_python(self) -> str:
        """The interpreter runs use — the override, or the venv deploy creates
        under the (resolved) remote dir."""
        return self.remote_python or f"{self.resolved_remote_dir()}/.venv/bin/python"

    def uses_managed_venv(self) -> bool:
        """True when we're using the auto-created venv (no explicit
        remote_python), so deploy should create it rather than assume it exists."""
        return not self.remote_python

    def is_configured(self) -> bool:
        """Enough set to attempt a run: just a host. The install dir and
        interpreter fall back to a managed venv under the remote home."""
        return bool(self.alias)


def _settings(settings: QSettings | None) -> QSettings:
    return settings if settings is not None else QSettings(_ORG, _APP)


# Stored under flat keys ("remote/alias", …) rather than one structured value:
# QSettings has its own per-type coercion (and collapses 1-element lists to a
# bare string), so primitive scalars round-trip most reliably.
def load_remote_config(settings: QSettings | None = None) -> RemoteConfig:
    store = _settings(settings)
    cfg = RemoteConfig()
    for f in fields(RemoteConfig):
        raw = store.value(f"{_PREFIX}/{f.name}", None)
        if raw is None:
            continue
        if f.type == "bool" or isinstance(getattr(cfg, f.name), bool):
            # QSettings may hand back the literal string "true"/"false" (INI
            # backend) instead of a real bool — normalise both.
            value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
        else:
            value = str(raw)
        setattr(cfg, f.name, value)
    return cfg


def save_remote_config(cfg: RemoteConfig, settings: QSettings | None = None) -> None:
    store = _settings(settings)
    for f in fields(RemoteConfig):
        store.setValue(f"{_PREFIX}/{f.name}", getattr(cfg, f.name))
