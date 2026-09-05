from pathlib import Path

import pytest

from isynca.config import (
    Config,
    default_config_path,
    default_data_dir,
    load,
    load_env,
    load_file,
)
from isynca.errors import ConfigError


def test_default_dirs_use_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "dat"))
    assert default_config_path() == tmp_path / "cfg" / "isynca" / "config.toml"
    assert default_data_dir() == tmp_path / "dat" / "isynca"


def test_default_dirs_fall_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_data_dir() == tmp_path / ".local/share" / "isynca"


def test_relative_xdg_value_is_ignored(monkeypatch, tmp_path):
    """The XDG spec requires absolute paths; a relative one is treated as unset."""
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_data_dir() == tmp_path / ".local/share" / "isynca"


def test_derived_paths(tmp_path):
    config = Config(data_dir=tmp_path)
    assert config.ledger_path == tmp_path / "ledger.db"
    assert config.cookie_dir == tmp_path / "cookies"


def test_with_overrides_ignores_none():
    config = Config(apple_id="a@example.com", min_size=3)
    updated = config.with_overrides(apple_id=None, min_size=5)
    assert updated.apple_id == "a@example.com"
    assert updated.min_size == 5


def test_with_overrides_rejects_unknown():
    with pytest.raises(ConfigError, match="Unknown setting"):
        Config().with_overrides(nonsense=1)


def test_load_file_missing_returns_empty(tmp_path):
    assert load_file(tmp_path / "absent.toml") == {}


def test_load_file_reads_isynca_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[isynca]\napple_id = "me@example.com"\nmin_size = 4\n')
    assert load_file(path) == {"apple_id": "me@example.com", "min_size": 4}


def test_load_file_accepts_bare_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('apple_id = "me@example.com"\n')
    assert load_file(path) == {"apple_id": "me@example.com"}


def test_load_file_rejects_invalid_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml")
    with pytest.raises(ConfigError, match="Could not read config file"):
        load_file(path)


def test_load_file_rejects_non_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('isynca = "a string"\n')
    with pytest.raises(ConfigError, match="must contain a table"):
        load_file(path)


def test_media_kinds_default_to_both_on():
    config = Config()
    assert config.videos
    assert config.images


def test_media_kinds_can_be_switched_off():
    assert Config().with_overrides(images=False).videos
    assert not Config().with_overrides(images=False).images


def test_load_env_reads_boolean_kind_toggles():
    assert load_env({"ISYNCA_VIDEOS": "no", "ISYNCA_IMAGES": "true"}) == {
        "videos": False,
        "images": True,
    }


def test_load_env_coerces_types():
    env = {
        "ISYNCA_APPLE_ID": "me@example.com",
        "ISYNCA_MIN_SIZE": "3",
        "ISYNCA_FOLLOW_SYMLINKS": "yes",
        "ISYNCA_VERBOSE": "0",
        "PATH": "/usr/bin",
        "ISYNCA_UNKNOWN": "ignored",
    }
    assert load_env(env) == {
        "apple_id": "me@example.com",
        "min_size": 3,
        "follow_symlinks": True,
        "verbose": False,
    }


def test_load_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("ISYNCA_APPLE_ID", "env@example.com")
    assert load_env()["apple_id"] == "env@example.com"


def test_load_env_rejects_bad_integer():
    with pytest.raises(ConfigError, match="must be an integer"):
        load_env({"ISYNCA_MIN_SIZE": "many"})


def test_load_layers_precedence(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[isynca]\napple_id = "file@example.com"\nmin_size = 1\n')
    config = load(
        config_path=path,
        environ={"ISYNCA_MIN_SIZE": "2"},
        apple_id="cli@example.com",
    )
    assert config.apple_id == "cli@example.com"  # CLI beats file
    assert config.min_size == 2  # env beats file


def test_load_converts_paths_and_sequences(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[isynca]\ndata_dir = "{tmp_path / "state"}"\nexclude = ["*.tmp", "*.part"]\n'
    )
    config = load(config_path=path, environ={})
    assert config.data_dir == tmp_path / "state"
    assert config.exclude == ("*.tmp", "*.part")


def test_load_expands_user_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text('[isynca]\ndata_dir = "~/state"\n')
    assert load(config_path=path, environ={}).data_dir == tmp_path / "state"


def test_load_rejects_unknown_file_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[isynca]\nnonsense = 1\n")
    with pytest.raises(ConfigError, match="Unknown setting"):
        load(config_path=path, environ={})


def test_load_uses_default_path_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = load(environ={})
    assert config.apple_id is None
