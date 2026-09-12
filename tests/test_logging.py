import logging

import pytest
from rich.console import Console

from isynca.errors import ConfigError
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


def test_a_full_log_file_gets_every_record(tmp_path):
    """Debug lines land in the file even when the terminal is not verbose."""
    log_file = tmp_path / "full.log"
    configure(console=Console(), log_file=log_file)
    get_logger("scanner").debug("looking at %s", "/tmp/x")
    get_logger("scanner").warning("cannot read %s", "/tmp/y")
    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("DEBUG    isynca.scanner: looking at /tmp/x")
    assert lines[1].endswith("WARNING  isynca.scanner: cannot read /tmp/y")


def test_a_full_log_file_does_not_make_the_terminal_verbose(tmp_path):
    logger = configure(console=Console(), log_file=tmp_path / "full.log")
    assert logger.level == logging.DEBUG
    terminal = logger.handlers[0]
    assert terminal.level == logging.INFO


def test_a_warn_log_gets_only_warnings_and_above(tmp_path):
    warn_log = tmp_path / "warn.log"
    configure(console=Console(), warn_log=warn_log)
    get_logger().debug("debug")
    get_logger().info("info")
    get_logger().warning("warning")
    get_logger().error("error")
    lines = warn_log.read_text().splitlines()
    assert len(lines) == 2
    assert "warning" in lines[0]
    assert "error" in lines[1]


def test_both_log_files_can_be_written_at_once(tmp_path):
    full, warn = tmp_path / "full.log", tmp_path / "warn.log"
    logger = configure(console=Console(), log_file=full, warn_log=warn)
    assert len(logger.handlers) == 3
    get_logger().info("info")
    get_logger().warning("warning")
    assert len(full.read_text().splitlines()) == 2
    assert len(warn.read_text().splitlines()) == 1


def test_log_files_are_appended_to(tmp_path):
    log_file = tmp_path / "full.log"
    configure(console=Console(), log_file=log_file)
    get_logger().info("first run")
    configure(console=Console(), log_file=log_file)
    get_logger().info("second run")
    lines = log_file.read_text().splitlines()
    assert [line.split(": ", 1)[1] for line in lines] == ["first run", "second run"]


def test_log_file_lines_carry_a_timestamp(tmp_path):
    log_file = tmp_path / "full.log"
    configure(console=Console(), log_file=log_file)
    get_logger().info("hello")
    line = log_file.read_text().rstrip("\n")
    # "YYYY-MM-DD HH:MM:SS,mmm" and then the level.
    assert line[4] == "-" and line[10] == " " and line[19] == ","
    assert line.endswith("INFO     isynca: hello")


def test_a_missing_log_directory_is_created(tmp_path):
    log_file = tmp_path / "deep" / "nested" / "full.log"
    configure(console=Console(), log_file=log_file)
    get_logger().info("hello")
    assert log_file.is_file()


def test_an_unwritable_log_file_is_a_config_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    with pytest.raises(ConfigError, match="Could not open log file"):
        configure(console=Console(), log_file=blocker / "full.log")


def test_reconfiguring_closes_the_previous_file_handlers(tmp_path):
    first = configure(console=Console(), log_file=tmp_path / "a.log")
    file_handler = first.handlers[1]
    assert isinstance(file_handler, logging.FileHandler)
    logger = configure(console=Console())
    assert file_handler not in logger.handlers
    assert file_handler.stream is None
