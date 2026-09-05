"""``isynca photos`` -- inventory local media and upload it to iCloud Photos."""

from __future__ import annotations

from pathlib import Path
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

from isynca.cli.context import get_context
from isynca.config import Config
from isynca.errors import ConfigError
from isynca.icloud import session as icloud_session
from isynca.icloud.photos import PhotosUploader
from isynca.ledger.store import Ledger, UploadStatus
from isynca.media.scanner import Scanner
from isynca.media.types import MediaKind
from isynca.sync.planner import PlannedUpload, Planner, UploadPlan
from isynca.sync.report import RunReport, format_bytes
from isynca.sync.runner import UploadRunner

app = typer.Typer(help="Work with iCloud Photos.", no_args_is_help=True)

SourceArg = Annotated[
    list[Path],
    typer.Argument(help="Folders or files to scan.", show_default=False),
]

# Both kinds are on by default; each flag pair lets one be switched off
# without having to re-state the other.
VideosOpt = Annotated[
    bool | None,
    typer.Option("--videos/--no-videos", help="Include video files."),
]
ImagesOpt = Annotated[
    bool | None,
    typer.Option("--images/--no-images", help="Include image files."),
]


@app.command("scan")
def scan(
    ctx: typer.Context,
    sources: SourceArg,
    videos: VideosOpt = None,
    images: ImagesOpt = None,
    min_size: Annotated[
        int | None,
        typer.Option("--min-size", help="Ignore files smaller than N bytes."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Glob to skip; repeatable."),
    ] = None,
    follow_symlinks: Annotated[
        bool, typer.Option("--follow-symlinks", help="Descend into symlinked dirs.")
    ] = False,
) -> None:
    """List the media that an upload would consider. Touches no network."""
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        videos=videos,
        images=images,
        min_size=min_size,
        exclude=tuple(exclude) if exclude else None,
        follow_symlinks=follow_symlinks or None,
    )
    scanner = _build_scanner(config)

    table = Table(title="Discovered media")
    table.add_column("File", overflow="fold")
    table.add_column("Kind")
    table.add_column("Size", justify="right")

    total = 0
    for media in scanner.scan(sources):
        total += media.size
        table.add_row(str(media.path), str(media.kind), format_bytes(media.size))

    app_ctx.console.print(table)
    app_ctx.console.print(
        f"{scanner.stats.matched} file(s), {format_bytes(total)}; "
        f"{scanner.stats.skipped} skipped of {scanner.stats.files_seen} seen."
    )


@app.command("upload")
def upload(
    ctx: typer.Context,
    sources: SourceArg,
    album: Annotated[
        str | None, typer.Option("--album", help="Album to upload into.")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would happen, upload nothing."),
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Upload at most N files.")
    ] = None,
    videos: VideosOpt = None,
    images: ImagesOpt = None,
    min_size: Annotated[
        int | None,
        typer.Option("--min-size", help="Ignore files smaller than N bytes."),
    ] = None,
    exclude: Annotated[
        list[str] | None, typer.Option("--exclude", help="Glob to skip; repeatable.")
    ] = None,
    follow_symlinks: Annotated[
        bool, typer.Option("--follow-symlinks", help="Descend into symlinked dirs.")
    ] = False,
) -> None:
    """Upload discovered media to iCloud Photos, skipping anything already sent."""
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        album=album,
        dry_run=dry_run or None,
        videos=videos,
        images=images,
        min_size=min_size,
        exclude=tuple(exclude) if exclude else None,
        follow_symlinks=follow_symlinks or None,
    )
    console = app_ctx.console
    scanner = _build_scanner(config)

    with Ledger(config.ledger_path) as ledger:
        plan = _build_plan(scanner, ledger, sources, limit, console)

        if not plan.pending:
            console.print("[green]Nothing to upload; everything is up to date.[/green]")
            _print_report(console, _empty_report(plan, config.dry_run))
            return

        console.print(
            f"{len(plan.pending)} file(s) to upload, "
            f"{format_bytes(plan.pending_bytes)} total."
        )

        uploader = None
        if not config.dry_run:
            api = icloud_session.connect(
                apple_id=app_ctx.require_apple_id(),
                cookie_dir=config.cookie_dir,
            )
            uploader = PhotosUploader(api, album=config.album)

        report = _execute(plan, uploader, ledger, config, console)

    _print_report(console, report)
    if not report.ok:
        raise typer.Exit(code=1)


def _build_scanner(config: Config) -> Scanner:
    """Build a scanner from resolved settings.

    Turning both kinds off leaves nothing to look for; the scanner rejects
    that rather than silently walking the tree and finding zero files.
    """
    kinds = frozenset(
        kind
        for kind, enabled in (
            (MediaKind.VIDEO, config.videos),
            (MediaKind.IMAGE, config.images),
        )
        if enabled
    )
    if not kinds:
        raise ConfigError("Nothing to scan for: --no-videos and --no-images cancel out")

    return Scanner(
        kinds=kinds,
        min_size=config.min_size,
        exclude=config.exclude,
        follow_symlinks=config.follow_symlinks,
    )


def _build_plan(
    scanner: Scanner,
    ledger: Ledger,
    sources: list[Path],
    limit: int | None,
    console: Console,
) -> UploadPlan:
    """Scan and plan, showing a spinner because hashing can take a while."""
    planner = Planner(ledger)
    plan = UploadPlan()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning and hashing...", total=None)
        for decision in planner.iter_plan(scanner.scan(sources)):
            if isinstance(decision, PlannedUpload):
                if limit is not None and len(plan.pending) >= limit:
                    continue
                plan.pending.append(decision)
            else:
                plan.skipped.append(decision)
            progress.update(task, description=f"Planned {plan.total} file(s)...")

    return plan


def _execute(
    plan: UploadPlan,
    uploader: PhotosUploader | None,
    ledger: Ledger,
    config: Config,
    console: Console,
) -> RunReport:
    """Run the plan with a progress bar covering the pending bytes."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Uploading", total=len(plan.pending))

        def advance(item: PlannedUpload, _status: UploadStatus | None) -> None:
            progress.update(task, advance=1, description=f"Uploading {item.path.name}")

        runner = UploadRunner(
            uploader=uploader,
            ledger=ledger,
            dry_run=config.dry_run,
            progress=advance,
        )
        return runner.run(plan)


def _empty_report(plan: UploadPlan, dry_run: bool) -> RunReport:
    """Build a report for a run with nothing to upload."""
    report = RunReport(dry_run=dry_run)
    for skipped in plan.skipped:
        report.record_skip(skipped.reason)
    return report


def _print_report(console: Console, report: RunReport) -> None:
    """Render the run summary, plus any failures."""
    table = Table(title="Summary", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    for label, value in report.summary_rows():
        table.add_row(label, value)
    console.print(table)

    if report.failures:
        failures = Table(title="Failures")
        failures.add_column("File", overflow="fold")
        failures.add_column("Error", overflow="fold")
        for failure in report.failures:
            failures.add_row(str(failure.path), failure.message)
        console.print(failures)
