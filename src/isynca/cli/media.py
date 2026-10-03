"""``isynca media`` -- check and fix up local media files, never touching iCloud."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from isynca.cli.context import AppContext, get_context
from isynca.cli.photos import (
    ExcludeOpt,
    ImagesOpt,
    SourceArg,
    SymlinksOpt,
    VideosOpt,
    build_scanner,
)
from isynca.errors import RotationError
from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger
from isynca.media.compat import Assessment, Verdict, assess, container_name
from isynca.media.probe import MediaInfo, probe
from isynca.media.rotate import QUARTER_TURNS, rotate_video
from isynca.media.types import MediaKind

app = typer.Typer(help="Check and fix up local media files.", no_args_is_help=True)

_VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.OK: "[green]ok        [/green]",
    Verdict.UNSURE: "[yellow]unsure    [/yellow]",
    Verdict.CONVERT: "[red]convert   [/red]",
    Verdict.UNREADABLE: "[bold red]unreadable[/bold red]",
}
"""Padded to one width so the paths after them line up."""


@app.command("check")
def check(
    ctx: typer.Context,
    sources: SourceArg,
    videos: VideosOpt = None,
    images: ImagesOpt = None,
    exclude: ExcludeOpt = None,
    follow_symlinks: SymlinksOpt = False,
) -> None:
    """Show what each file holds and whether iCloud Photos is likely to take it.

    Reads the format, codecs, size and date taken of each file, then judges
    it: ok, unsure, convert, or unreadable, with the reasons. Nothing is
    changed and nothing is sent anywhere. Videos need ffprobe, from ffmpeg.
    """
    app_ctx = get_context(ctx)
    config = app_ctx.config.with_overrides(
        videos=videos,
        images=images,
        exclude=tuple(exclude) if exclude else None,
        follow_symlinks=follow_symlinks or None,
    )
    scanner = build_scanner(config)

    verdicts: Counter[Verdict] = Counter()
    undated = 0
    # The spinner shares the logging console, so a warning raised mid-scan
    # lands above it rather than through it.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=app_ctx.err_console,
        transient=True,
    ) as progress:
        task = progress.add_task("Checking...", total=None)
        for media in scanner.scan(sources):
            info = probe(media)
            assessment = assess(info)
            verdicts[assessment.verdict] += 1
            undated += assessment.verdict is not Verdict.UNREADABLE and not info.taken
            _print_file(app_ctx.console, info, assessment)
            progress.update(task, description=f"Checked {verdicts.total()} file(s)...")

    if not verdicts:
        app_ctx.console.print("No media found.")
        return
    app_ctx.console.print(
        f"{verdicts.total()} file(s): {verdicts[Verdict.OK]} ok, "
        f"{verdicts[Verdict.UNSURE]} unsure, {verdicts[Verdict.CONVERT]} to "
        f"convert, {verdicts[Verdict.UNREADABLE]} unreadable."
    )
    if undated:
        app_ctx.console.print(
            f"{undated} file(s) carry no date taken; iCloud will most likely "
            f"file them under the day they are uploaded."
        )


def _print_file(console: Console, info: MediaInfo, assessment: Assessment) -> None:
    """Print one checked file: verdict and path, then what it holds and why.

    A block per file rather than a table row: paths and reasons are long, and
    a table folds them into narrow columns that are hard to read.
    """
    console.print(f"{_VERDICT_LABELS[assessment.verdict]} {info.media.path}")
    if info.error is None:
        console.print(f"  {_describe(info)}", highlight=False)
    for reason in assessment.reasons:
        console.print(f"  [dim]-[/dim] {reason}", highlight=False)


def _describe(info: MediaInfo) -> str:
    """Return a one-line summary of format, size and date taken."""
    parts = [container_name(info)]
    if info.media.kind is MediaKind.VIDEO:
        video = info.video_codec or "no video"
        if info.video_profile:
            video += f" ({info.video_profile})"
        parts.append(video)
        parts.append(info.audio_codec or "no audio")
    if info.width and info.height:
        size = f"{info.width}x{info.height}"
        parts.append(f"{size} turned {info.rotation}°" if info.rotation else size)
    parts.append(
        f"taken {info.taken:%Y-%m-%d %H:%M}"
        if info.taken
        else "[red]no date taken[/red]"
    )
    return " · ".join(parts)


@app.command("rotate")
def rotate(
    ctx: typer.Context,
    files: Annotated[
        list[Path],
        typer.Argument(help="MP4/MOV videos to rotate.", show_default=False),
    ],
    clockwise: Annotated[
        int,
        typer.Option(
            "--clockwise",
            help="Degrees to turn clockwise: 90, 180 or 270.",
            show_default=False,
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be written, writing nothing."),
    ] = False,
) -> None:
    """Write an upright copy of each video, without re-encoding it.

    Each copy goes beside its original as NAME_rot<degrees>.EXT; the original
    is left untouched. Only the display matrix differs, so there is no quality
    loss and the capture date carries over.
    """
    if clockwise not in QUARTER_TURNS:
        raise typer.BadParameter("must be 90, 180 or 270", param_hint="--clockwise")

    app_ctx = get_context(ctx)
    failed = False
    with app_ctx.open_ledger() as ledger:
        for path in files:
            try:
                _rotate_one(app_ctx, ledger, path, clockwise, dry_run)
            except RotationError as exc:
                app_ctx.err_console.print(f"[red]Cannot rotate {path}:[/red] {exc}")
                failed = True
    if failed:
        raise typer.Exit(code=1)


def _rotate_one(
    app_ctx: AppContext, ledger: Ledger, path: Path, clockwise: int, dry_run: bool
) -> None:
    """Rotate one file and report it, warning if iCloud holds the old version."""
    # Check the file can be rotated before paying to hash it.
    planned = rotate_video(path, clockwise, dry_run=True)
    uploaded = _uploaded_at(ledger, path)
    change = rotate_video(path, clockwise) if not dry_run else planned

    verb = "Would write" if dry_run else "Wrote"
    app_ctx.console.print(
        f"{verb} {change.output} ({change.before}° → {change.after}°)"
    )
    if uploaded is not None:
        app_ctx.console.print(
            f"  [yellow]The unrotated version was uploaded to iCloud Photos on "
            f"{uploaded}.[/yellow] Uploading the rotated copy adds it as a new "
            f"item; delete the old one in Photos."
        )


def _uploaded_at(ledger: Ledger, path: Path) -> str | None:
    """Return when the ledger says this exact content was uploaded, if ever.

    The stat cache answers for a path an upload run has already seen;
    anything else is hashed, so a file uploaded from another path or after a
    rename is still recognised.
    """
    try:
        stat = path.stat()
        digest = ledger.cached_hash(path, stat.st_size, stat.st_mtime_ns)
        record = ledger.lookup(digest or hash_file(path))
    except OSError as exc:
        raise RotationError(str(exc), retryable=False) from exc
    return None if record is None else f"{record.uploaded_at:%Y-%m-%d}"
