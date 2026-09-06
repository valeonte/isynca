"""Shared state handed from the root callback down to each subcommand."""

from __future__ import annotations

from dataclasses import dataclass, field

import typer
from rich.console import Console

from isynca.config import Config
from isynca.errors import ConfigError
from isynca.ledger.store import Ledger
from isynca.notify import NotifySession


@dataclass(frozen=True, slots=True)
class AppContext:
    """Configuration and output consoles for one invocation.

    Two consoles, split the way the streams are: ``console`` carries the
    command's results -- tables and summaries -- on stdout, while
    ``err_console`` carries progress bars and log lines on stderr. Rich can
    only keep a live progress display from colliding with other output when
    both go through one console object, so this same ``err_console`` is what
    :func:`isynca.logging.configure` is given.

    ``notify`` is the third output stream, and the slowest: one desktop
    notification summarising the whole run, sent as the process exits.
    """

    config: Config
    console: Console
    err_console: Console = field(default_factory=lambda: Console(stderr=True))
    notify: NotifySession = field(default_factory=NotifySession)
    """This run's desktop notification, still being written.

    Inert unless the root callback armed it, so a command can hand it
    whatever it likes without checking whether anyone is listening.
    """

    def open_ledger(self) -> Ledger:
        """Open the ledger for this invocation's data directory."""
        return Ledger(self.config.ledger_path)

    def require_apple_id(self) -> str:
        """Return the configured Apple ID, or fail with clear guidance."""
        if not self.config.apple_id:
            raise ConfigError(
                "No Apple ID configured. Run 'isynca auth login --apple-id "
                "you@example.com' once, or pass --apple-id, set "
                "ISYNCA_APPLE_ID, or add apple_id to your config.toml."
            )
        return self.config.apple_id


def get_context(ctx: typer.Context) -> AppContext:
    """Return the :class:`AppContext` attached by the root callback."""
    obj = ctx.obj
    if not isinstance(obj, AppContext):  # pragma: no cover - defensive
        raise ConfigError("Application context was not initialised")
    return obj
