import logging

from rich.console import Console

from isynca.logging import LOGGER_NAME, configure, get_logger
from isynca.notify import NotificationCollector


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


def test_a_collector_is_attached_alongside_the_rich_handler():
    collector = NotificationCollector()
    logger = configure(console=Console(), collector=collector)
    assert collector in logger.handlers
    assert len(logger.handlers) == 2


def test_warnings_reach_an_attached_collector():
    collector = NotificationCollector()
    configure(console=Console(), collector=collector)
    get_logger("scanner").warning("cannot read %s", "/tmp/x")
    assert collector.total == 1
    assert collector.body() == "cannot read /tmp/x"


def test_reconfiguring_does_not_keep_the_previous_collector():
    first = NotificationCollector()
    configure(console=Console(), collector=first)
    logger = configure(console=Console())
    assert first not in logger.handlers
