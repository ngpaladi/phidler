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


def test_unset_config_is_defaults(tmp_path):
    """A user who never configured a remote gets a blank, not-configured config."""
    loaded = load_remote_config(_throwaway_settings(tmp_path))
    assert loaded == RemoteConfig()
    assert loaded.is_configured() is False


def test_is_configured_needs_alias_and_python():
    assert RemoteConfig(alias="h", remote_python="p").is_configured() is True
    assert RemoteConfig(alias="h").is_configured() is False
    assert RemoteConfig(remote_python="p").is_configured() is False
