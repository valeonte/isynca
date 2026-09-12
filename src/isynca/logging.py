"""Logging setup built on rich, shared by every command."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from isynca.errors import ConfigError

LOGGER_NAME = "isynca"

FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
"""Layout of one line in a log file.

Every line carries a timestamp because the files are appended to, run after
run, and the timestamps are what tell one run apart from the next.
"""


def configure(
    verbose: bool = False,
    console: Console | None = None,
    log_file: Path | None = None,
    warn_log: Path | None = None,
) -> logging.Logger:
    """Install the isynca logger's handlers and return it.

    Repeated calls replace the existing handlers rather than stacking a second
    set, so a command that reconfigures logging does not double every line.

    The terminal gets a rich handler showing ``INFO`` and above, or everything
    when ``verbose``. ``log_file`` appends every record, debug included,
    whatever the terminal shows; ``warn_log`` appends only warnings and
    errors. Either, both, or neither may be given.
    """
    logger = logging.getLogger(LOGGER_NAME)
    # The logger itself only gates what the handlers are offered, so it has
    # to be as permissive as the most talkative handler: a full log file
    # needs the debug lines even when the terminal is not showing them.
    logger.setLevel(logging.DEBUG if verbose or log_file else logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = RichHandler(
        console=console or Console(stderr=True),
        show_path=verbose,
        show_time=verbose,
        rich_tracebacks=True,
        markup=False,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(handler)
    if log_file is not None:
        logger.addHandler(_file_handler(log_file, logging.DEBUG))
    if warn_log is not None:
        logger.addHandler(_file_handler(warn_log, logging.WARNING))
    return logger


def _file_handler(path: Path, level: int) -> logging.Handler:
    """Return a handler appending records at ``level`` and above to ``path``.

    The parent directory is created so that a log path pointing into a
    not-yet-existing folder works on the first run, the way most log setups
    are expected to.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not open log file {path}: {exc}") from exc
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    return handler


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the isynca logger, or a named child of it."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")
