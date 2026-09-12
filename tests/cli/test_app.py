import click
import pytest
import typer
from rich.console import Console
from typer.core import TyperCommand

import isynca.__main__ as entry
from isynca import __version__
from isynca.cli.app import app, main
from isynca.cli.context import AppContext, get_context
from isynca.config import Config
from isynca.errors import ConfigError


def test_version_flag(runner):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_every_command_group(runner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("auth", "photos", "ledger"):
        assert name in result.output


def test_no_args_shows_help(runner):
    assert runner.invoke(app, []).exit_code != 0


def test_context_is_populated(invoke, data_dir):
    result = invoke("ledger", "stats")
    assert result.exit_code == 0
    assert "Files in stat cache" in result.output


def test_verbose_flag_is_accepted(invoke):
    assert invoke("--verbose", "ledger", "stats").exit_code == 0


def test_apple_id_flows_from_the_root_option(invoke):
    result = invoke("--apple-id", "me@example.com", "auth", "logout")
    assert result.exit_code == 0


def test_require_apple_id_raises_when_unset(data_dir):

    ctx = AppContext(config=Config(data_dir=data_dir), console=Console())
    with pytest.raises(ConfigError, match="No Apple ID configured"):
        ctx.require_apple_id()


def test_require_apple_id_returns_the_value(data_dir):

    ctx = AppContext(
        config=Config(apple_id="me@x.com", data_dir=data_dir), console=Console()
    )
    assert ctx.require_apple_id() == "me@x.com"


def test_open_ledger_uses_the_configured_path(data_dir):

    ctx = AppContext(config=Config(data_dir=data_dir), console=Console())
    with ctx.open_ledger() as ledger:
        assert ledger.path == data_dir / "ledger.db"


def test_main_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr("isynca.cli.app.app", lambda **kwargs: None)
    assert main() == 0


def test_main_reports_isynca_errors(monkeypatch, capsys):
    def boom(**kwargs):
        raise ConfigError("bad config")

    monkeypatch.setattr("isynca.cli.app.app", boom)
    assert main() == 1
    assert "bad config" in capsys.readouterr().err


def test_main_propagates_a_raised_typer_exit(monkeypatch):
    def boom(**kwargs):
        raise typer.Exit(code=3)

    monkeypatch.setattr("isynca.cli.app.app", boom)
    assert main() == 3


def test_main_propagates_a_returned_exit_code(monkeypatch):
    """Click returns the code for typer.Exit when standalone_mode is off.

    CliRunner runs in standalone mode, so only this test covers the path the
    installed console script actually takes.
    """
    monkeypatch.setattr("isynca.cli.app.app", lambda **kwargs: 4)
    assert main() == 4


def test_main_ignores_a_non_integer_return(monkeypatch):
    monkeypatch.setattr("isynca.cli.app.app", lambda **kwargs: "done")
    assert main() == 0


def test_main_renders_click_exceptions(monkeypatch, capsys):
    def boom(**kwargs):
        raise click.ClickException("bad usage")

    monkeypatch.setattr("isynca.cli.app.app", boom)
    assert main() == 1
    assert "bad usage" in capsys.readouterr().err


def test_main_handles_abort(monkeypatch, capsys):
    def boom(**kwargs):
        raise click.exceptions.Abort

    monkeypatch.setattr("isynca.cli.app.app", boom)
    assert main() == 130
    assert "Aborted" in capsys.readouterr().err


def test_get_context_rejects_a_missing_context():
    ctx = typer.Context(TyperCommand("x"))
    ctx.obj = None
    with pytest.raises(ConfigError, match="not initialised"):
        get_context(ctx)


def test_module_entry_point_is_importable():
    assert entry.main is main


def test_log_file_option_writes_the_full_log(invoke, tmp_path):
    log_file = tmp_path / "full.log"
    assert invoke("--log-file", str(log_file), "ledger", "stats").exit_code == 0
    assert log_file.is_file()


def test_warn_log_option_writes_only_problems(invoke, tmp_path):
    """A clean run leaves the warnings file empty rather than missing."""
    warn_log = tmp_path / "warn.log"
    assert invoke("--warn-log", str(warn_log), "ledger", "stats").exit_code == 0
    assert warn_log.read_text() == ""


def test_log_paths_in_the_environment_are_honoured(invoke, tmp_path, monkeypatch):
    log_file = tmp_path / "full.log"
    monkeypatch.setenv("ISYNCA_LOG_FILE", str(log_file))
    assert invoke("ledger", "stats").exit_code == 0
    assert log_file.is_file()


def test_an_unwritable_log_file_is_reported(invoke, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    result = invoke("--log-file", str(blocker / "x.log"), "ledger", "stats")
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
