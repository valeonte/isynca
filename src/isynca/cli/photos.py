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

from isynca.cli.context import AppContext, get_context
from isynca.config import Config
from isynca.errors import ConfigError
from isynca.icloud import session as icloud_session
from isynca.icloud.photos import PhotosUploader
from isynca.ledger.store import Ledger, UploadStatus
from isynca.media.capture import read_capture_date
from isynca.media.scanner import Scanner
from isynca.media.types import MediaKind
from isynca.sync.archiver import Archiver, prune_empty_dirs, validate_target
from isynca.sync.planner import PlannedUpload, Planner, SkipReason, UploadPlan
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
AlbumOpt = Annotated[str | None, typer.Option("--album", help="Album to upload into.")]
LimitOpt = Annotated[
    int | None, typer.Option("--limit", help="Handle at most N new files.")
]
MinSizeOpt = Annotated[
    int | None, typer.Option("--min-size", help="Ignore files smaller than N bytes.")
]
ExcludeOpt = Annotated[
    list[str] | None, typer.Option("--exclude", help="Glob to skip; repeatable.")
]
SymlinksOpt = Annotated[
    bool, typer.Option("--follow-symlinks", help="Descend into symlinked dirs.")
]
RequireDateOpt = Annotated[
    bool | None,
    typer.Option(
        "--require-date-taken/--no-require-date-taken",
        help="Report and hold back files with no capture date.",
    ),
]
PruneOpt = Annotated[
    bool | None,
    typer.Option(
        "--prune-empty-dirs/--no-prune-empty-dirs",
        help="Remove folders left empty under the sources. [default: prune]",
    ),
]


@app.command("scan")
def scan(
    ctx: typer.Context,
    sources: SourceArg,
    videos: VideosOpt = None,
    images: ImagesOpt = None,
    require_date_taken: RequireDateOpt = None,
    min_size: MinSizeOpt = None,
    exclude: ExcludeOpt = None,
    follow_symlinks: SymlinksOpt = False,
) -> None:
    """List the media that an upload would consider. Touches no network.

    With ``--require-date-taken`` each file's capture date is shown, so the
    files an upload would hold back can be audited before anything is sent.
    """
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        videos=videos,
        images=images,
        require_date_taken=require_date_taken,
        min_size=min_size,
        exclude=tuple(exclude) if exclude else None,
        follow_symlinks=follow_symlinks or None,
    )
    scanner = _build_scanner(config)
    checking_dates = config.require_date_taken

    table = Table(title="Discovered media")
    table.add_column("File", overflow="fold")
    table.add_column("Kind")
    table.add_column("Size", justify="right")
    if checking_dates:
        table.add_column("Date taken")

    total = 0
    undated = 0
    for media in scanner.scan(sources):
        total += media.size
        row = [str(media.path), str(media.kind), format_bytes(media.size)]
        if checking_dates:
            taken = read_capture_date(media)
            undated += taken is None
            row.append(
                taken.strftime("%Y-%m-%d %H:%M") if taken else "[red]missing[/red]"
            )
        table.add_row(*row)

    app_ctx.console.print(table)
    app_ctx.console.print(
        f"{scanner.stats.matched} file(s), {format_bytes(total)}; "
        f"{scanner.stats.skipped} skipped of {scanner.stats.files_seen} seen."
    )
    if checking_dates:
        app_ctx.console.print(f"{undated} file(s) with no capture date.")


@app.command("upload")
def upload(
    ctx: typer.Context,
    sources: SourceArg,
    album: AlbumOpt = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would happen, upload nothing."),
    ] = False,
    limit: LimitOpt = None,
    videos: VideosOpt = None,
    images: ImagesOpt = None,
    require_date_taken: RequireDateOpt = None,
    min_size: MinSizeOpt = None,
    exclude: ExcludeOpt = None,
    follow_symlinks: SymlinksOpt = False,
) -> None:
    """Upload discovered media to iCloud Photos, skipping anything already sent."""
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        album=album,
        dry_run=dry_run or None,
        videos=videos,
        images=images,
        require_date_taken=require_date_taken,
        min_size=min_size,
        exclude=tuple(exclude) if exclude else None,
        follow_symlinks=follow_symlinks or None,
    )
    _run(app_ctx, sources, config, limit, archiver=None)


@app.command("archive")
def archive(
    ctx: typer.Context,
    sources: SourceArg,
    to: Annotated[
        Path,
        typer.Option(
            "--to",
            help="Folder to move successfully archived media into.",
            show_default=False,
        ),
    ],
    album: AlbumOpt = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would happen, change nothing."),
    ] = False,
    limit: LimitOpt = None,
    videos: VideosOpt = None,
    images: ImagesOpt = None,
    require_date_taken: RequireDateOpt = None,
    min_size: MinSizeOpt = None,
    exclude: ExcludeOpt = None,
    follow_symlinks: SymlinksOpt = False,
    prune_empty_dirs: PruneOpt = None,
) -> None:
    """Upload media, then move what iCloud holds into another folder.

    Each file that iCloud confirms -- newly uploaded or already there -- is
    moved under the target, keeping its path relative to the source it was
    found under. A file whose upload was accepted but not yet indexed is left
    where it is, and an existing file at the destination is never overwritten.

    Folders under the sources that the move leaves empty are removed;
    ``--no-prune-empty-dirs`` keeps the empty structure standing. The sources
    themselves are never removed.
    """
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        album=album,
        dry_run=dry_run or None,
        videos=videos,
        images=images,
        require_date_taken=require_date_taken,
        min_size=min_size,
        exclude=tuple(exclude) if exclude else None,
        follow_symlinks=follow_symlinks or None,
        prune_empty_dirs=prune_empty_dirs,
    )
    validate_target(to, sources)
    _run(app_ctx, sources, config, limit, archiver=Archiver(to, config.dry_run))


def _run(
    app_ctx: AppContext,
    sources: list[Path],
    config: Config,
    limit: int | None,
    archiver: Archiver | None,
) -> None:
    """Scan, plan, upload, and optionally archive, then report."""
    console = app_ctx.console
    scanner = _build_scanner(config)

    with Ledger(config.ledger_path) as ledger:
        plan = _build_plan(
            scanner,
            ledger,
            sources,
            limit,
            app_ctx.err_console,
            config.require_date_taken,
        )
        _print_missing_dates(console, plan)

        # An archive run still has work when nothing is pending: files
        # uploaded on an earlier run are waiting to be moved out.
        if not plan.pending and archiver is None:
            console.print("[green]Nothing to upload; everything is up to date.[/green]")
            _finish(app_ctx, _empty_report(plan, config.dry_run))
            return

        if plan.pending:
            console.print(
                f"{len(plan.pending)} file(s) to upload, "
                f"{format_bytes(plan.pending_bytes)} total."
            )

        uploader = None
        if plan.pending and not config.dry_run:
            api = icloud_session.connect(
                apple_id=app_ctx.require_apple_id(),
                cookie_dir=config.cookie_dir,
            )
            uploader = PhotosUploader(api, album=config.album)

        report = _execute(plan, uploader, ledger, config, app_ctx.err_console, archiver)

    _prune(sources, config, archiver, report)
    _finish(app_ctx, report)
    if not report.ok:
        raise typer.Exit(code=1)


def _prune(
    sources: list[Path],
    config: Config,
    archiver: Archiver | None,
    report: RunReport,
) -> None:
    """Clear the folders an archive run emptied, if pruning is on.

    Only an archive run empties anything, so an upload never prunes however
    the setting is left; the folders it would find empty were empty before it
    ran and are none of its business.
    """
    if archiver is None or not config.prune_empty_dirs:
        return
    report.pruning = True
    emptied = prune_empty_dirs(sources, dry_run=config.dry_run, moved=archiver.moved)
    report.record_prune(len(emptied))


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
    require_date_taken: bool = False,
) -> UploadPlan:
    """Scan and plan, showing a spinner because hashing can take a while.

    The spinner draws on the console logging shares, so a warning raised while
    hashing lands above it rather than through it.
    """
    planner = Planner(ledger, require_capture_date=require_date_taken)
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
    archiver: Archiver | None,
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

        def starting(item: PlannedUpload) -> None:
            progress.update(task, description=f"Uploading {item.path.name}")

        def advance(_item: PlannedUpload, _status: UploadStatus | None) -> None:
            progress.update(task, advance=1)

        runner = UploadRunner(
            uploader=uploader,
            ledger=ledger,
            dry_run=config.dry_run,
            archiver=archiver,
            on_start=starting,
            progress=advance,
        )
        return runner.run(plan)


def _print_missing_dates(console: Console, plan: UploadPlan) -> None:
    """List the files held back for having no capture date.

    A count in the summary is not actionable; the point of the switch is to
    find out which files need attention.
    """
    undated = [s.media for s in plan.skipped if s.reason is SkipReason.MISSING_DATE]
    if not undated:
        return

    table = Table(title="No capture date - not uploaded")
    table.add_column("File", overflow="fold")
    table.add_column("Kind")
    for media in undated:
        table.add_row(str(media.path), str(media.kind))
    console.print(table)


def _empty_report(plan: UploadPlan, dry_run: bool) -> RunReport:
    """Build a report for a run with nothing to upload."""
    report = RunReport(dry_run=dry_run)
    for skipped in plan.skipped:
        report.record_skip(skipped.reason)
    return report


def _finish(app_ctx: AppContext, report: RunReport) -> None:
    """Print the run summary, and offer the same story to the desktop.

    Whether anything is actually shown is the session's call: a run short
    enough to have been watched says nothing unless it logged a problem.
    """
    _print_report(app_ctx.console, report)
    headline, details = _completion_text(report)
    app_ctx.notify.finished(headline, details)


def _completion_text(report: RunReport) -> tuple[str, str]:
    """Return the headline and one-line detail for a finished run."""
    if report.dry_run:
        headline = "Dry run finished"
    elif report.archiving:
        headline = "Archive finished"
    else:
        headline = "Upload finished"

    parts = [f"{report.uploaded} uploaded"]
    if report.archiving:
        parts.append(f"{report.moved} moved")
    if report.skipped:
        parts.append(f"{report.skipped} skipped")
    if report.failed:
        parts.append(f"{report.failed} failed")
    return headline, ", ".join(parts)


def _print_report(console: Console, report: RunReport) -> None:
    """Render the run summary, plus any failures."""
    table = Table(title="Summary", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    for label, value in report.summary_rows():
        table.add_row(label, value)
    console.print(table)

    if not report.ok:
        failures = Table(title="Failures")
        failures.add_column("File", overflow="fold")
        failures.add_column("Error", overflow="fold")
        for failure in report.all_failures():
            failures.add_row(str(failure.path), failure.message)
        console.print(failures)
