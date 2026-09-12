"""``isynca files`` -- inspect and transfer iCloud Drive contents.

This is the reconnaissance half of the file sync: enough to see what an
account actually holds, and to put a single file somewhere, without any of
the reconciliation logic that decides to delete things.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from isynca.cli.context import AppContext, get_context
from isynca.config import Config
from isynca.errors import ConfigError
from isynca.files.client import DriveClient
from isynca.files.local import ExcludeRules, LocalScanner, LocalTree
from isynca.files.planner import Direction, SyncAction, SyncPlan, SyncPlanner
from isynca.files.report import SyncReport
from isynca.files.runner import SyncRunner, check_deletion_threshold, remote_index
from isynca.files.state import SyncState
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


DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", help="Report what would happen, change nothing."),
]
PushOpt = Annotated[
    bool,
    typer.Option("--push-only", help="Apply local changes to iCloud; ignore its own."),
]
PullOpt = Annotated[
    bool,
    typer.Option("--pull-only", help="Apply iCloud's changes locally; ignore local."),
]
MaxDeletesOpt = Annotated[
    int | None,
    typer.Option("--max-deletes", help="Refuse a run deleting more than N items."),
]
ForceOpt = Annotated[
    bool,
    typer.Option("--force", help="Proceed past the deletion limit."),
]
ExcludeOpt = Annotated[
    list[str] | None,
    typer.Option("--exclude", help="Glob to skip, on both sides; repeatable."),
]


@app.command("sync")
def sync(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Argument(
            help="Local folder mirroring your iCloud Drive root.",
            show_default=False,
        ),
    ],
    dry_run: DryRunOpt = False,
    push_only: PushOpt = False,
    pull_only: PullOpt = False,
    max_deletes: MaxDeletesOpt = None,
    force: ForceOpt = False,
    exclude: ExcludeOpt = None,
    include_app_libraries: AppLibrariesOpt = False,
) -> None:
    """Sync a local folder against iCloud Drive, both ways.

    New files move in whichever direction they appeared, edits follow
    whichever side made them, and a file deleted on one side is deleted on
    the other. What changed on *both* sides since the last run is never
    guessed at: it is reported and left alone.

    The first run against a folder has nothing recorded to compare with, so
    files that already match by size are adopted rather than transferred, and
    nothing is deleted -- there is no earlier agreement for a deletion to be
    a departure from.
    """
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        dry_run=dry_run or None,
        max_deletes=max_deletes,
        exclude=tuple(exclude) if exclude else None,
        include_app_libraries=include_app_libraries or None,
    )
    if not root.expanduser().is_dir():
        raise ConfigError(f"Not a folder: {root}")

    direction = _direction(push_only, pull_only)
    client = _connect(app_ctx)

    with SyncState(config.sync_state_path) as state:
        local, remote = _survey(client, root, config, app_ctx.err_console)
        plan = SyncPlanner(direction=direction).plan(local, remote, state.records(root))
        check_deletion_threshold(plan, config.max_deletes, force)
        _print_conflicts(app_ctx.console, plan)

        report = _execute(
            plan, remote, client, state, root, config, app_ctx.err_console
        )

    report.direction = str(direction)
    _print_report(app_ctx.console, report)
    if not report.ok:
        raise typer.Exit(code=1)


def _direction(push_only: bool, pull_only: bool) -> Direction:
    """Resolve the direction flags into a single mode."""
    if push_only and pull_only:
        raise ConfigError("--push-only and --pull-only cancel out")
    if push_only:
        return Direction.PUSH
    if pull_only:
        return Direction.PULL
    return Direction.BOTH


def _survey(
    client: DriveClient, root: Path, config: Config, console: Console
) -> tuple[LocalTree, dict[PurePosixPath, RemoteNode]]:
    """Read both sides, behind a spinner because the walk is the slow part."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Reading local folder...", total=None)
        rules = ExcludeRules(config.exclude)
        local = LocalScanner(exclude=rules).scan(root)
        progress.update(task, description="Listing iCloud Drive...")
        remote = remote_index(
            client.walk(
                include_app_libraries=config.include_app_libraries,
                skip=rules.matches,
            )
        )
    return local, remote


def _execute(
    plan: SyncPlan,
    remote: dict[PurePosixPath, RemoteNode],
    client: DriveClient,
    state: SyncState,
    root: Path,
    config: Config,
    console: Console,
) -> SyncReport:
    """Run the plan behind a progress bar covering its actions."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        actionable = len(plan.actions) - len(plan.conflicts)
        task = progress.add_task("Syncing", total=actionable)

        def advance(action: SyncAction) -> None:
            progress.update(task, advance=1, description=f"{action.kind} {action.path}")

        runner = SyncRunner(
            client=client,
            state=state,
            root=root,
            dry_run=config.dry_run,
            progress=advance,
        )
        return runner.run(plan, remote)


def _print_conflicts(console: Console, plan: SyncPlan) -> None:
    """List the paths the run refuses to touch, and why."""
    if not plan.conflicts:
        return
    table = Table(title="Conflicts - not synced")
    table.add_column("Path", overflow="fold")
    table.add_column("Why", overflow="fold")
    for action in plan.conflicts:
        table.add_row(str(action.path), action.detail)
    console.print(table)


def _print_report(console: Console, report: SyncReport) -> None:
    """Render the sync summary, plus any failures."""
    table = Table(title="Sync summary", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    for label, value in report.summary_rows():
        table.add_row(label, value)
    console.print(table)

    if report.failures:
        failures = Table(title="Failures")
        failures.add_column("Path", overflow="fold")
        failures.add_column("Error", overflow="fold")
        for failure in report.failures:
            failures.add_row(str(failure.path), failure.message)
        console.print(failures)
