"""``isynca files`` -- inspect and transfer iCloud Drive contents.

This is the reconnaissance half of the file sync: enough to see what an
account actually holds, and to put a single file somewhere, without any of
the reconciliation logic that decides to delete things.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from rich.table import Table

from isynca.cli.context import AppContext, get_context
from isynca.errors import ConfigError
from isynca.files.client import DriveClient
from isynca.files.types import RemoteNode
from isynca.icloud import session as icloud_session
from isynca.sync.report import format_bytes

app = typer.Typer(help="Work with iCloud Drive.", no_args_is_help=True)

DepthOpt = Annotated[
    int,
    typer.Option("--depth", help="Levels to descend; 0 for the whole tree."),
]
AppLibrariesOpt = Annotated[
    bool,
    typer.Option(
        "--include-app-libraries",
        help="Include the per-app folders (Pages, Numbers, ...) at the root.",
    ),
]


def _connect(app_ctx: AppContext) -> DriveClient:
    """Authenticate and return a Drive client."""
    api = icloud_session.connect(
        apple_id=app_ctx.require_apple_id(),
        cookie_dir=app_ctx.config.cookie_dir,
    )
    return DriveClient(api)


@app.command("list")
def list_nodes(
    ctx: typer.Context,
    depth: DepthOpt = 1,
    include_app_libraries: AppLibrariesOpt = False,
) -> None:
    """List what iCloud Drive holds, without changing anything.

    The raw node type is shown alongside the classified kind, because Apple
    returns more types than the two a sync acts on and knowing which is which
    is the whole point of looking.
    """
    app_ctx = get_context(ctx)
    client = _connect(app_ctx)

    table = Table(title="iCloud Drive")
    table.add_column("Path", overflow="fold")
    table.add_column("Type")
    table.add_column("Size", justify="right")
    table.add_column("Modified", no_wrap=True)
    table.add_column("Etag", no_wrap=True)

    files = folders = 0
    total = 0
    for node in client.walk(include_app_libraries=include_app_libraries, depth=depth):
        if node.is_dir:
            folders += 1
        else:
            files += 1
            total += node.size or 0
        table.add_row(
            str(node.path),
            node.raw_type,
            "-" if node.size is None else format_bytes(node.size),
            node.modified.strftime("%Y-%m-%d %H:%M") if node.modified else "-",
            node.etag or "-",
        )

    app_ctx.console.print(table)
    app_ctx.console.print(
        f"{files} file(s), {format_bytes(total)}; {folders} folder(s)."
    )


@app.command("put")
def put(
    ctx: typer.Context,
    source: Annotated[
        Path,
        typer.Argument(help="Local file to upload.", show_default=False),
    ],
    to: Annotated[
        str,
        typer.Option("--to", help="Remote folder to upload into."),
    ] = ".",
) -> None:
    """Upload one local file into a folder of iCloud Drive.

    A single-file primitive, not a sync: it uploads unconditionally and does
    not look at what is already there.
    """
    app_ctx = get_context(ctx)
    if not source.is_file():
        raise ConfigError(f"Not a file: {source}")

    client = _connect(app_ctx)
    parent = _resolve_folder(client, to)
    client.upload(parent, source)
    app_ctx.console.print(f"[green]Uploaded {source.name} to {parent.path}.[/green]")


def _resolve_folder(client: DriveClient, remote: str) -> RemoteNode:
    """Return the folder named by a remote path, walking down from the root."""
    node = client.root()
    # PurePosixPath drops "." components itself, so the default "." resolves
    # to no parts at all and leaves the root standing.
    for part in PurePosixPath(remote).parts:
        matches = [
            child
            for child in client.children(node)
            if child.name == part and child.is_dir
        ]
        if not matches:
            raise ConfigError(f"No such folder in iCloud Drive: {remote}")
        node = matches[0]
    return node
