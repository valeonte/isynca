"""``isynca auth`` -- sign in, inspect, and drop the stored session."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from isynca.cli.context import get_context
from isynca.icloud import session as icloud_session

app = typer.Typer(help="Manage the stored iCloud session.", no_args_is_help=True)

AppleIdOpt = Annotated[
    str | None, typer.Option("--apple-id", help="Apple ID to act on.")
]


@app.command("login")
def login(
    ctx: typer.Context,
    apple_id: AppleIdOpt = None,
    store_password: Annotated[
        bool,
        typer.Option("--store-password", help="Save the password in the keyring."),
    ] = False,
) -> None:
    """Authenticate, completing two-factor authentication interactively."""
    app_ctx = get_context(ctx)
    account = apple_id or app_ctx.require_apple_id()
    console = app_ctx.console

    password = typer.prompt(f"Password for {account}", hide_input=True)

    icloud_session.connect(
        apple_id=account,
        password=password,
        cookie_dir=app_ctx.config.cookie_dir,
        interactive=True,
        code_prompt=lambda: typer.prompt("Two-factor code"),
    )

    if store_password:
        icloud_session.save_password(account, password)
        console.print("Password saved to the system keyring.")

    # Only after the connection succeeded, so a failed attempt does not leave
    # a bogus account behind for later commands to default to.
    icloud_session.remember_account(app_ctx.config.data_dir, account)
    console.print(f"[green]Signed in as {account}.[/green]")


@app.command("status")
def status(ctx: typer.Context, apple_id: AppleIdOpt = None) -> None:
    """Report whether the stored session can be used without prompting."""
    app_ctx = get_context(ctx)
    account = apple_id or app_ctx.require_apple_id()
    result = icloud_session.status(account, cookie_dir=app_ctx.config.cookie_dir)

    table = Table(title=f"Session for {result.apple_id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Authenticated", _yes_no(result.authenticated))
    table.add_row("Trusted session", _yes_no(result.trusted))
    table.add_row("Needs 2FA", _yes_no(result.requires_2fa))
    table.add_row("Password in keyring", _yes_no(result.password_stored))
    table.add_row("Ready to use", _yes_no(result.usable))
    app_ctx.console.print(table)

    if not result.usable:
        app_ctx.console.print("Run 'isynca auth login' to refresh this session.")
        raise typer.Exit(code=1)


@app.command("logout")
def logout(ctx: typer.Context, apple_id: AppleIdOpt = None) -> None:
    """Delete the stored session cookies, keyring password, and account."""
    app_ctx = get_context(ctx)
    account = apple_id or app_ctx.require_apple_id()

    removed = icloud_session.clear_cookies(app_ctx.config.cookie_dir)
    had_password = icloud_session.forget_password(account)
    icloud_session.forget_account(app_ctx.config.data_dir)

    app_ctx.console.print(f"Removed {removed} cookie file(s).")
    app_ctx.console.print(
        "Removed the stored password."
        if had_password
        else "No stored password to remove."
    )


def _yes_no(value: bool) -> str:
    """Render a boolean as coloured yes/no."""
    return "[green]yes[/green]" if value else "[red]no[/red]"
