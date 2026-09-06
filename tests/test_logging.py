import logging

from rich.console import Console

from isynca.logging import LOGGER_NAME, configure, get_logger


def test_configure_installs_single_handler():
    first = configure(console=Console())
    second = configure(console=Console())
    assert first is second
    assert len(second.handlers) == 1


def test_verbose_sets_debug_level():
    assert configure(verbose=True, console=Console()).level == logging.DEBUG
    assert configure(verbose=False, console=Console()).level == logging.INFO


def test_configure_defaults_to_stderr_console():
    logger = configure()
    assert logger.handlers


def test_get_logger_returns_root_and_children():
    assert get_logger().name == LOGGER_NAME
    assert get_logger("scanner").name == f"{LOGGER_NAME}.scanner"


def test_logger_does_not_propagate():
    assert configure(console=Console()).propagate is False
