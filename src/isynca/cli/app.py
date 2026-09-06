"""Typer application: global options, subcommand wiring, and error handling."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console

from isynca import __version__
from isynca.cli import auth as auth_cli
from isynca.cli import ledger as ledger_cli
from isynca.cli import photos as photos_cli
from isynca.cli.context import AppContext
from isynca.config import load
from isynca.errors import IsyncaError
from isynca.icloud import session as icloud_session
from isynca.logging import configure

app = typer.Typer(
    name="isynca",
    help="Modular iCloud syncing toolkit.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(auth_cli.app, name="auth")
app.add_typer(photos_cli.app, name="photos")
app.add_typer(ledger_cli.app, name="ledger")


def _version_callback(value: bool) -> None:
    """Print the version and exit when ``--version`` was passed."""
    if value:
        Console().print(f"isynca {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to config.toml."),
    ] = None,
    apple_id: Annotated[
        str | None,
        typer.Option("--apple-id", help="Apple ID to operate as."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Directory for the ledger and cookies."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable debug logging."),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Resolve configuration once and share it with every subcommand."""
    settings = load(
        config_path=config,
        apple_id=apple_id,
        data_dir=data_dir,
        verbose=verbose or None,
    )
    if settings.apple_id is None:
        # Lowest precedence: an explicit --apple-id, ISYNCA_APPLE_ID, or a
        # config.toml entry all still win. This only saves you from repeating
        # the account on every command after signing in once.
        settings = settings.with_overrides(
            apple_id=icloud_session.recall_account(settings.data_dir)
        )
    # One stderr console for both logging and progress bars: rich keeps a
    # live display and printed output from overwriting each other only when
    # they share a console, and two of them is what smears log lines across
    # the upload bar.
    err_console = Console(stderr=True)
    configure(verbose=settings.verbose, console=err_console)
    ctx.obj = AppContext(config=settings, console=Console(), err_console=err_console)


def main() -> int:
    """Console-script entry point that renders isynca errors politely.

    ``standalone_mode=False`` keeps click from calling ``sys.exit`` so isynca
    errors can be rendered as one clean line. The catch is that click then
    *returns* the code for a ``typer.Exit`` instead of raising it, so the
    return value has to be honoured or a failing command would report success.
    """
    try:
        result = app(standalone_mode=False)
    except IsyncaError as exc:
        Console(stderr=True).print(f"[red]Error:[/red] {exc}")
        return 1
    except typer.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        Console(stderr=True).print("[yellow]Aborted.[/yellow]")
        return 130
    return result if isinstance(result, int) else 0
