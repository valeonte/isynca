"""Shared state handed from the root callback down to each subcommand."""

from __future__ import annotations

from dataclasses import dataclass

import typer
from rich.console import Console

from isynca.config import Config
from isynca.errors import ConfigError
from isynca.ledger.store import Ledger


@dataclass(frozen=True, slots=True)
class AppContext:
    """Configuration and output console for one invocation."""

    config: Config
    console: Console

    def open_ledger(self) -> Ledger:
        """Open the ledger for this invocation's data directory."""
        return Ledger(self.config.ledger_path)

    def require_apple_id(self) -> str:
        """Return the configured Apple ID, or fail with clear guidance."""
        if not self.config.apple_id:
            raise ConfigError(
                "No Apple ID configured. Pass --apple-id, set ISYNCA_APPLE_ID, "
                "or add apple_id to your config.toml."
            )
        return self.config.apple_id


def get_context(ctx: typer.Context) -> AppContext:
    """Return the :class:`AppContext` attached by the root callback."""
    obj = ctx.obj
    if not isinstance(obj, AppContext):  # pragma: no cover - defensive
        raise ConfigError("Application context was not initialised")
    return obj
