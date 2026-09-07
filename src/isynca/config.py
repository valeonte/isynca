"""Configuration: XDG-based paths, TOML file loading, and layered overrides.

Precedence, lowest to highest: built-in defaults, the TOML config file,
environment variables, then explicit command-line options.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from isynca.errors import ConfigError

APP_NAME = "isynca"
ENV_PREFIX = "ISYNCA_"

NOTIFY_LEVELS = ("WARNING", "ERROR", "CRITICAL")
"""Levels that may be notified about. Anything quieter is normal progress."""


def _xdg_dir(env_var: str, default: str) -> Path:
    """Return an XDG base directory, honouring the environment variable.

    An empty or relative value is treated as unset, matching the XDG spec,
    which requires absolute paths.
    """
    raw = os.environ.get(env_var, "")
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / default
    return base / APP_NAME


def default_config_path() -> Path:
    """Return the default location of ``config.toml``."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "config.toml"


def default_data_dir() -> Path:
    """Return the directory holding the ledger and session cookies."""
    return _xdg_dir("XDG_DATA_HOME", ".local/share")


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved settings for a single isynca invocation."""

    apple_id: str | None = None
    data_dir: Path = field(default_factory=default_data_dir)
    album: str | None = None
    videos: bool = True
    images: bool = True
    require_date_taken: bool = False
    min_size: int = 0
    follow_symlinks: bool = False
    exclude: tuple[str, ...] = ()
    dry_run: bool = False
    prune_empty_dirs: bool = True
    max_deletes: int = 50
    include_app_libraries: bool = False
    verbose: bool = False
    notify: bool = True
    notify_level: str = "WARNING"

    def __post_init__(self) -> None:
        """Normalise and check the notification level.

        Done here rather than at the CLI so that a level coming from the
        config file or the environment is caught just as early.
        """
        level = str(self.notify_level).upper()
        if level not in NOTIFY_LEVELS:
            raise ConfigError(
                f"notify_level must be one of {', '.join(NOTIFY_LEVELS)}, "
                f"not {self.notify_level!r}"
            )
        object.__setattr__(self, "notify_level", level)

    @property
    def ledger_path(self) -> Path:
        """Return the path of the SQLite upload ledger."""
        return self.data_dir / "ledger.db"

    @property
    def cookie_dir(self) -> Path:
        """Return the directory pyicloud stores session cookies in."""
        return self.data_dir / "cookies"

    @property
    def sync_state_path(self) -> Path:
        """Return the path of the iCloud Drive sync state database.

        Kept apart from the photos ledger: that one is keyed by content hash
        for a one-way flow, this one by path for a two-way one, and a single
        file pretending to be both would serve neither.
        """
        return self.data_dir / "drive.db"

    def with_overrides(self, **overrides: Any) -> Config:  # noqa: ANN401
        """Return a copy with every non-``None`` override applied."""
        supplied = {key: value for key, value in overrides.items() if value is not None}
        unknown = set(supplied) - set(self.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        return replace(self, **supplied)


_FIELD_TYPES: dict[str, type] = {
    "apple_id": str,
    "album": str,
    "videos": bool,
    "images": bool,
    "require_date_taken": bool,
    "min_size": int,
    "follow_symlinks": bool,
    "prune_empty_dirs": bool,
    "max_deletes": int,
    "include_app_libraries": bool,
    "verbose": bool,
    "notify": bool,
    "notify_level": str,
}


def _coerce(key: str, value: str) -> Any:  # noqa: ANN401
    """Convert an environment variable string to the field's declared type."""
    field_type = _FIELD_TYPES.get(key)
    if field_type is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if field_type is int:
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{key.upper()} must be an integer") from exc
    return value


def load_file(path: Path) -> dict[str, Any]:
    """Load settings from a TOML file, returning ``{}`` when it is absent."""
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc

    settings = data.get("isynca", data)
    if not isinstance(settings, dict):
        raise ConfigError(f"Config file {path} must contain a table of settings")
    return settings


def load_env(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect ``ISYNCA_*`` environment variables into a settings mapping."""
    source = os.environ if environ is None else environ
    settings: dict[str, Any] = {}
    for name, value in source.items():
        if not name.startswith(ENV_PREFIX):
            continue
        key = name.removeprefix(ENV_PREFIX).lower()
        if key in Config.__dataclass_fields__:
            settings[key] = _coerce(key, value)
    return settings


def load(
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
    **cli_overrides: Any,  # noqa: ANN401
) -> Config:
    """Build a :class:`Config` from file, environment, and CLI layers."""
    path = config_path or default_config_path()
    settings: dict[str, Any] = {}
    settings.update(load_file(path))
    settings.update(load_env(environ))

    unknown = set(settings) - set(Config.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    if "data_dir" in settings:
        settings["data_dir"] = Path(settings["data_dir"]).expanduser()
    if "exclude" in settings:
        settings["exclude"] = tuple(settings["exclude"])

    return Config(**settings).with_overrides(**cli_overrides)
