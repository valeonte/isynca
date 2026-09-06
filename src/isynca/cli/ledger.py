"""``isynca ledger`` -- inspect and edit the local record of uploads."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from isynca.cli.context import get_context
from isynca.ledger.hashing import hash_file
from isynca.sync.report import format_bytes

app = typer.Typer(help="Inspect the local upload ledger.", no_args_is_help=True)


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    """Summarise what the ledger has recorded."""
    app_ctx = get_context(ctx)
    with app_ctx.open_ledger() as ledger:
        data = ledger.stats()

    # The path goes on its own line rather than in the table title: a long
    # path wraps badly inside a narrow, content-fitted table.
    app_ctx.console.print(f"Ledger: {app_ctx.config.ledger_path}")

    table = Table(title="Ledger contents", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Confirmed", str(data["confirmed"]))
    table.add_row("Uploaded, not yet indexed", str(data["unverified"]))
    table.add_row("Known duplicates", str(data["duplicate"]))
    table.add_row("Total uploads", str(data["total"]))
    table.add_row("Bytes recorded", format_bytes(data["bytes"]))
    table.add_row("Files in stat cache", str(data["tracked_files"]))
    app_ctx.console.print(table)


@app.command("list")
def list_records(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Show at most N records.")] = 50,
) -> None:
    """List recorded uploads, newest first."""
    app_ctx = get_context(ctx)
    with app_ctx.open_ledger() as ledger:
        records = ledger.records()[:limit]

    if not records:
        app_ctx.console.print("The ledger is empty.")
        return

    table = Table(title="Recorded uploads")
    table.add_column("Uploaded", no_wrap=True)
    table.add_column("Status")
    table.add_column("Size", justify="right")
    table.add_column("Path", overflow="fold")
    for record in records:
        table.add_row(
            record.uploaded_at.strftime("%Y-%m-%d %H:%M"),
            str(record.status),
            format_bytes(record.size),
            str(record.first_path),
        )
    app_ctx.console.print(table)


@app.command("forget")
def forget(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="File to drop from the ledger.")],
) -> None:
    """Forget one file so the next run uploads it again.

    The path is looked up in the stat cache first; if it is not there but the
    file still exists, its content is hashed so the record can still be found
    after a move or rename.
    """
    app_ctx = get_context(ctx)
    with app_ctx.open_ledger() as ledger:
        removed = ledger.forget_path(path)
        if not removed and path.is_file():
            removed = ledger.forget(hash_file(path))

    if removed:
        app_ctx.console.print(f"[green]Forgot {path}.[/green]")
        return
    app_ctx.console.print(f"No ledger entry for {path}.")
    raise typer.Exit(code=1)


@app.command("prune")
def prune(ctx: typer.Context) -> None:
    """Drop stat-cache rows for files that no longer exist on disk."""
    app_ctx = get_context(ctx)
    with app_ctx.open_ledger() as ledger:
        removed = ledger.prune()
    app_ctx.console.print(f"Pruned {removed} stale cache row(s).")
