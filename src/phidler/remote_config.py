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


@dataclass
class RemoteConfig:
    """Where and how to run an offloaded FDTD job.

    `alias` is a host alias from the user's ~/.ssh/config (phidler shells out to
    `ssh <alias> …` and lets SSH config + agent/keys handle auth — it stores no
    secrets). `remote_dir` is where phidler rsyncs the phidler + photonfdtd
    source checkouts and installs them. `remote_python` is the venv interpreter
    on the remote where they're pip-installed (so `<remote_python> -m
    phidler.fdtd_subprocess` resolves). `use_gpu` requests the remote GPU
    backend independently of the local machine's GPU. `local_photonfdtd_dir`
    overrides where deploy finds the local photonfdtd checkout to rsync — needed
    because a user offloading FDTD may have no importable local photonfdtd."""

    alias: str = ""
    remote_dir: str = ""
    remote_python: str = ""
    use_gpu: bool = False
    local_photonfdtd_dir: str = ""

    def is_configured(self) -> bool:
        """Enough set to attempt a run: a host and an interpreter to run."""
        return bool(self.alias and self.remote_python)


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
