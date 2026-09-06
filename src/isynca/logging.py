"""Logging setup built on rich, shared by every command."""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "isynca"


def configure(
    verbose: bool = False,
    console: Console | None = None,
    collector: logging.Handler | None = None,
) -> logging.Logger:
    """Install a rich handler on the isynca logger and return it.

    Repeated calls replace the existing handlers rather than stacking a second
    set, so a command that reconfigures logging does not double every line.

    ``collector`` is attached alongside the rich handler and follows the same
    rule. It is how :class:`isynca.notify.session.NotifySession` gets to see
    every warning and error without any call site knowing it exists.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = RichHandler(
        console=console or Console(stderr=True),
        show_path=verbose,
        show_time=verbose,
        rich_tracebacks=True,
        markup=False,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    if collector is not None:
        logger.addHandler(collector)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the isynca logger, or a named child of it."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")
