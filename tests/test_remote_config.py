"""Per-user remote-server config round-trips through QSettings."""

from PySide6.QtCore import QSettings

from phidler.remote_config import RemoteConfig, load_remote_config, save_remote_config


def _throwaway_settings(tmp_path):
    """An INI-backed QSettings in a tmp file, so tests never touch the real
    per-user store."""
    return QSettings(str(tmp_path / "remote.ini"), QSettings.IniFormat)


def test_round_trips_all_fields(tmp_path):
    settings = _throwaway_settings(tmp_path)
    cfg = RemoteConfig(
        alias="gpubox",
        remote_dir="~/phidler-remote",
        remote_python="~/phidler-remote/.venv/bin/python",
        use_gpu=True,
        local_photonfdtd_dir="/home/me/photonfdtd",
    )
    save_remote_config(cfg, settings)

    loaded = load_remote_config(settings)
    assert loaded == cfg
    assert loaded.use_gpu is True  # bool survives the INI string coercion


def test_use_gpu_false_round_trips(tmp_path):
    settings = _throwaway_settings(tmp_path)
    save_remote_config(RemoteConfig(alias="h", remote_python="p", use_gpu=False), settings)
    assert load_remote_config(settings).use_gpu is False


def test_backend_field_round_trips(tmp_path):
    settings = _throwaway_settings(tmp_path)
    save_remote_config(RemoteConfig(alias="h", backend="jax"), settings)
    loaded = load_remote_config(settings)
    assert loaded.backend == "jax"
    assert loaded.resolved_backend() == "jax"


def test_resolved_backend_maps_legacy_use_gpu_to_jax():
    # A config saved by an older phidler had only use_gpu (no `backend`); it meant
    # the GPU, which is now JAX's GPU build (CuPy was removed in photonfdtd 0.13).
    assert RemoteConfig(alias="h", use_gpu=True).resolved_backend() == "jax"
    assert RemoteConfig(alias="h").resolved_backend() == "cpu"
    # An explicit backend wins over the legacy bool.
    assert RemoteConfig(alias="h", backend="jax", use_gpu=True).resolved_backend() == "jax"
    assert RemoteConfig(alias="h", backend="cpu", use_gpu=True).resolved_backend() == "cpu"


def test_resolved_backend_maps_removed_cupy_to_jax(tmp_path):
    assert RemoteConfig(alias="h", backend="cupy").resolved_backend() == "jax"
    # ...including after a save/load round trip of an old config.
    settings = _throwaway_settings(tmp_path)
    save_remote_config(RemoteConfig(alias="h", backend="cupy", use_gpu=True), settings)
    assert load_remote_config(settings).resolved_backend() == "jax"


def test_unset_config_is_defaults(tmp_path):
    """A user who never configured a remote gets a blank, not-configured config."""
    loaded = load_remote_config(_throwaway_settings(tmp_path))
    assert loaded == RemoteConfig()
    assert loaded.is_configured() is False


def test_is_configured_needs_only_a_host():
    # A host alone is enough now — install dir + interpreter are derived.
    assert RemoteConfig(alias="h").is_configured() is True
    assert RemoteConfig().is_configured() is False
    assert RemoteConfig(remote_python="p").is_configured() is False  # no host


def test_derived_dir_and_python_from_a_bare_host():
    from phidler.remote_config import DEFAULT_REMOTE_DIR

    cfg = RemoteConfig(alias="h")
    assert cfg.resolved_remote_dir() == DEFAULT_REMOTE_DIR
    assert cfg.resolved_remote_python() == f"{DEFAULT_REMOTE_DIR}/.venv/bin/python"
    assert cfg.uses_managed_venv() is True


def test_overrides_win_over_the_derived_defaults():
    cfg = RemoteConfig(alias="h", remote_dir="~/custom", remote_python="/opt/py/bin/python")
    assert cfg.resolved_remote_dir() == "~/custom"
    assert cfg.resolved_remote_python() == "/opt/py/bin/python"
    assert cfg.uses_managed_venv() is False  # explicit interpreter -> deploy won't create it


def test_derived_python_follows_a_custom_dir():
    cfg = RemoteConfig(alias="h", remote_dir="~/custom")
    assert cfg.resolved_remote_python() == "~/custom/.venv/bin/python"
    assert cfg.uses_managed_venv() is True


def test_each_host_keeps_its_own_settings(tmp_path):
    from phidler.remote_config import load_host_config, select_remote_host

    settings = _throwaway_settings(tmp_path)
    save_remote_config(RemoteConfig(alias="gpubox", backend="jax", remote_dir="/data/ph"), settings)
    save_remote_config(RemoteConfig(alias="cluster.edu", backend="cpu"), settings)
    assert load_remote_config(settings).alias == "cluster.edu"

    # Switching back restores what was saved for that host and makes it current.
    chosen = select_remote_host("gpubox", settings)
    assert chosen == RemoteConfig(alias="gpubox", backend="jax", remote_dir="/data/ph")
    assert load_remote_config(settings) == chosen
    assert load_host_config("cluster.edu", settings).backend == "cpu"


def test_selecting_a_new_host_starts_from_defaults(tmp_path):
    from phidler.remote_config import select_remote_host

    settings = _throwaway_settings(tmp_path)
    save_remote_config(RemoteConfig(alias="gpubox", backend="jax", remote_python="/x/py"), settings)
    assert select_remote_host("fresh", settings) == RemoteConfig(alias="fresh")
    assert load_remote_config(settings) == RemoteConfig(alias="fresh")
