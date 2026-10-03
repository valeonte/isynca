"""``isynca media`` -- check and fix up local media files, never touching iCloud."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from isynca.cli.context import AppContext, get_context
from isynca.cli.photos import (
    ExcludeOpt,
    ImagesOpt,
    SourceArg,
    SymlinksOpt,
    VideosOpt,
    build_scanner,
)
from isynca.errors import ConvertError, DateError, RotationError
from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger
from isynca.media.capture import read_capture_date
from isynca.media.compat import Assessment, Verdict, assess, container_name
from isynca.media.convert import (
    CONVERTED_SUFFIX,
    Conversion,
    convert,
    converted_path,
)
from isynca.media.dating import (
    DATED_SUFFIX,
    dated_path,
    modification_time,
    write_date,
)
from isynca.media.naming import NamePattern, parse_timezone
from isynca.media.probe import MediaInfo, probe
from isynca.media.rotate import QUARTER_TURNS, rotate_video
from isynca.media.types import MediaFile, MediaKind

app = typer.Typer(help="Check and fix up local media files.", no_args_is_help=True)

_VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.OK: "[green]ok        [/green]",
    Verdict.UNSURE: "[yellow]unsure    [/yellow]",
    Verdict.CONVERT: "[red]convert   [/red]",
    Verdict.UNREADABLE: "[bold red]unreadable[/bold red]",
}
"""Padded to one width so the paths after them line up."""

DateFromNameOpt = Annotated[
    str | None,
    typer.Option(
        "--date-from-name",
        help=(
            "For files with no date taken, read it from the file name by this "
            "pattern instead of using the modification time, e.g. "
            "'%y-%m-%d_%H-%M.%S'."
        ),
        show_default=False,
    ),
]
TimezoneOpt = Annotated[
    str | None,
    typer.Option(
        "--timezone",
        help=(
            "Zone for dates that carry none -- from --date-from-name, or a "
            "--date without an offset -- e.g. Europe/Athens or +03:00. "
            "Defaults to this machine's."
        ),
        show_default=False,
    ),
]


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

    checked: list[tuple[MediaInfo, Assessment]] = []
    # The spinner shares the logging console, so a warning raised mid-scan
    # lands above it rather than through it. Results go to stdout only once
    # it has gone: printed beside a live spinner on stderr, a line lands on
    # the spinner's own row and gets mangled.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=app_ctx.err_console,
        transient=True,
        disable=not app_ctx.err_console.is_terminal,
    ) as progress:
        task = progress.add_task("Checking...", total=None)
        for media in scanner.scan(sources):
            info = probe(media)
            checked.append((info, assess(info)))
            progress.update(task, description=f"Checked {len(checked)} file(s)...")

    verdicts: Counter[Verdict] = Counter()
    undated = 0
    for info, assessment in checked:
        _print_file(app_ctx.console, info, assessment)
        verdicts[assessment.verdict] += 1
        undated += assessment.verdict is not Verdict.UNREADABLE and not info.taken

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
        f"taken {_local(info.taken)}" if info.taken else "[red]no date taken[/red]"
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
            except (RotationError, OSError) as exc:
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


@app.command("fix-date")
def fix_date(
    ctx: typer.Context,
    sources: SourceArg,
    date: Annotated[
        str | None,
        typer.Option(
            "--date",
            help=(
                "Date taken to write instead of the modification time, e.g. "
                "2009-07-20T15:30 or 2009-07-20T15:30+03:00. One file only."
            ),
            show_default=False,
        ),
    ] = None,
    date_from_name: DateFromNameOpt = None,
    timezone: TimezoneOpt = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be written, writing nothing."),
    ] = False,
) -> None:
    """Write a dated copy of files that have no date taken.

    By default the date written is the file's modification time, and only
    files with no date taken are touched. --date writes the given date
    instead, replacing any existing one, and takes a single file. Each copy
    goes beside its original as NAME_dated.EXT; the original is left
    untouched. Works on JPEG images and MP4/MOV/3GP videos, losslessly.

    --date-from-name reads the date of undated files from their names
    instead of using the modification time, e.g. '%y-%m-%d_%H-%M.%S' for
    capture3.06-06-30_20-47.00.avi; a file whose name does not match is
    reported. --timezone says which zone such dates are in.
    """
    override, names = _date_options(date, date_from_name, timezone, sources)
    app_ctx = get_context(ctx)
    scanner = build_scanner(app_ctx.config)

    seen = False
    failed = False
    with app_ctx.open_ledger() as ledger:
        for media in scanner.scan(sources):
            seen = True
            try:
                _fix_one(app_ctx, ledger, media, override, dry_run, names)
            except (DateError, OSError) as exc:
                app_ctx.err_console.print(f"[red]Cannot date {media.path}:[/red] {exc}")
                failed = True
    if not seen:
        app_ctx.console.print("No media found.")
    if failed:
        raise typer.Exit(code=1)


def _date_options(
    date: str | None,
    date_from_name: str | None,
    timezone: str | None,
    sources: list[Path],
) -> tuple[datetime | None, NamePattern | None]:
    """Return the explicit date and the name pattern the options ask for."""
    if date is not None and date_from_name is not None:
        raise typer.BadParameter(
            "cannot be combined with --date", param_hint="--date-from-name"
        )
    zone = None
    if timezone is not None:
        if date is None and date_from_name is None:
            raise typer.BadParameter(
                "only applies with --date or --date-from-name", param_hint="--timezone"
            )
        try:
            zone = parse_timezone(timezone)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--timezone") from exc

    override = _parse_override(date, sources, zone) if date is not None else None
    names = None
    if date_from_name is not None:
        try:
            names = NamePattern.parse(date_from_name, zone)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--date-from-name") from exc
    return override, names


def _parse_override(
    text: str, sources: list[Path], zone: tzinfo | None = None
) -> datetime:
    """Return the ``--date`` value as an aware datetime, in ``zone`` if unzoned.

    One date for many files would stamp a whole folder with the same moment,
    which is never what anyone means, so it is refused outright.
    """
    if len(sources) != 1 or not sources[0].is_file():
        raise typer.BadParameter("takes exactly one file", param_hint="--date")
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter(
            "expected a date like 2009-07-20T15:30 or 2009-07-20T15:30+03:00",
            param_hint="--date",
        ) from exc
    if when.tzinfo is not None:
        return when
    return when.replace(tzinfo=zone) if zone is not None else when.astimezone()


def _fix_one(
    app_ctx: AppContext,
    ledger: Ledger,
    media: MediaFile,
    override: datetime | None,
    dry_run: bool,
    names: NamePattern | None = None,
) -> None:
    """Date one file and report it, warning if iCloud holds the undated version."""
    before = read_capture_date(media)
    if override is None and before is not None:
        app_ctx.console.print(
            f"[dim]Skipped {media.path}: already taken {_local(before)}[/dim]"
        )
        return

    when = override or _fallback_date(media, names)
    # Check the file can be dated before paying to hash it.
    planned = write_date(media, when, dry_run=True)
    uploaded = _uploaded_at(ledger, media.path)
    change = planned if dry_run else write_date(media, when)

    verb = "Would write" if dry_run else "Wrote"
    origin = "given date" if override else "name" if names else "modification time"
    line = f"{verb} {change.output} (taken {when:%Y-%m-%d %H:%M %z}, from the {origin}"
    if before is not None:
        line += f"; was {_local(before)}"
    app_ctx.console.print(line + ")", highlight=False)
    if uploaded is not None:
        app_ctx.console.print(
            f"  [yellow]The original was uploaded to iCloud Photos on "
            f"{uploaded}.[/yellow] Uploading the dated copy adds it as a new "
            f"item; delete the old one in Photos."
        )


@app.command("fix")
def fix(
    ctx: typer.Context,
    sources: SourceArg,
    convert_unsure: Annotated[
        bool,
        typer.Option(
            "--convert-unsure",
            help="Also convert files that play on some Apple devices only.",
        ),
    ] = False,
    date: Annotated[
        str | None,
        typer.Option(
            "--date",
            help=(
                "Date taken to give the result instead of its own or the "
                "modification time, e.g. 2009-07-20T15:30+03:00. One file only."
            ),
            show_default=False,
        ),
    ] = None,
    date_from_name: DateFromNameOpt = None,
    timezone: TimezoneOpt = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be written, writing nothing."),
    ] = False,
) -> None:
    """Make media ready for iCloud Photos, writing new files beside the old.

    Files iCloud will not take are converted into NAME_converted.mp4 (video)
    or NAME_converted.jpg (images), dated with their own date taken or, if
    they have none, their modification time. Files that need no converting
    but have no date taken get a dated copy, NAME_dated.EXT, as with
    fix-date. Files judged unsure are only dated unless --convert-unsure is
    given. Originals are never modified, and files already fixed by an
    earlier run are skipped, so an interrupted run can simply be repeated.

    --date takes a single file and gives its result that date instead,
    replacing any date it already has, whether it is converted or only dated.

    --date-from-name reads the date of files that have none from their names
    instead of using the modification time, as with fix-date.
    """
    override, names = _date_options(date, date_from_name, timezone, sources)
    app_ctx = get_context(ctx)
    scanner = build_scanner(app_ctx.config)

    seen = False
    failed = False
    with app_ctx.open_ledger() as ledger:
        for media in scanner.scan(sources):
            seen = True
            try:
                _fix_media(
                    app_ctx, ledger, media, convert_unsure, override, names, dry_run
                )
            except (ConvertError, DateError, OSError) as exc:
                app_ctx.err_console.print(f"[red]Cannot fix {media.path}:[/red] {exc}")
                failed = True
    if not seen:
        app_ctx.console.print("No media found.")
    if failed:
        raise typer.Exit(code=1)


def _fix_media(
    app_ctx: AppContext,
    ledger: Ledger,
    media: MediaFile,
    convert_unsure: bool,
    override: datetime | None,
    names: NamePattern | None,
    dry_run: bool,
) -> None:
    """Convert or date one file, whichever it needs, and report it.

    Re-running over a folder must neither redo nor compound earlier work, so
    a file an earlier run wrote is left alone, and so is a file whose output
    for this run already exists. An explicit ``override`` date is a request
    about one named file, so none of that applies: an existing output is
    reported as an error instead of being quietly skipped.
    """
    explicit = override is not None
    if not explicit and media.path.stem.endswith((CONVERTED_SUFFIX, DATED_SUFFIX)):
        _skip(app_ctx, media, "written by an earlier fix")
        return

    info = probe(media)
    assessment = assess(info)
    if assessment.verdict is Verdict.UNREADABLE:
        raise ConvertError("; ".join(assessment.reasons), retryable=False)

    unsure = assessment.verdict is Verdict.UNSURE
    if assessment.verdict is not Verdict.CONVERT and not (unsure and convert_unsure):
        if not explicit and dated_path(media.path).exists():
            _skip(app_ctx, media, f"{dated_path(media.path).name} already exists")
            return
        _fix_one(app_ctx, ledger, media, override, dry_run, names)
        if unsure:
            app_ctx.console.print(
                f"  [yellow]Not converted, though it may not play everywhere:"
                f"[/yellow] {'; '.join(assessment.reasons)}. "
                f"--convert-unsure converts it.",
                highlight=False,
            )
        return

    if not explicit and converted_path(media).exists():
        _skip(app_ctx, media, f"{converted_path(media).name} already exists")
        return

    named = override is None and names is not None and info.taken is None
    date = _fallback_date(media, names) if named else override
    # Check the file can be converted before paying to hash it.
    planned = convert(info, date=date, dry_run=True)
    uploaded = _uploaded_at(ledger, media.path)
    if dry_run:
        change = planned
    else:
        with Progress(
            TextColumn(f"Converting {media.path.name}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=app_ctx.err_console,
            transient=True,
            disable=not app_ctx.err_console.is_terminal,
        ) as progress:
            task = progress.add_task("convert", total=1.0)
            change = convert(
                info,
                date=date,
                on_progress=lambda done: progress.update(task, completed=done),
            )

    origin = "the given date" if explicit else "its name" if named else None
    _report_conversion(app_ctx, change, origin, dry_run)
    if uploaded is not None:
        app_ctx.console.print(
            f"  [yellow]The original was uploaded to iCloud Photos on "
            f"{uploaded}.[/yellow] Uploading the converted copy adds it as a "
            f"new item; delete the old one in Photos."
        )


def _report_conversion(
    app_ctx: AppContext, change: Conversion, origin: str | None, dry_run: bool
) -> None:
    """Print what a conversion did, or would do.

    ``origin`` names where a supplied date came from. Such a date is shown in
    its own zone, offset included -- an Athens time read from a name should
    read as written, not shifted to this machine's clock.
    """
    if origin is None:
        origin = "modification time" if change.dated_from_mtime else "its own date"
        taken = _local(change.taken)
    else:
        taken = f"{change.taken:%Y-%m-%d %H:%M %z}"
    verb = "Would convert" if dry_run else "Converted"
    line = (
        f"{verb} {change.source} → {change.output.name} (taken {taken}, from {origin}"
    )
    if change.copied:
        line += f"; {' and '.join(change.copied)} copied as is"
    app_ctx.console.print(line + ")", highlight=False)


def _fallback_date(media: MediaFile, names: NamePattern | None) -> datetime:
    """Return the date for a file that has none: from its name, or its mtime.

    A pattern was given because the modification times are known to be
    wrong, so a name it does not match is an error rather than a reason to
    fall back to them.
    """
    if names is None:
        return modification_time(media.path)
    found = names.date_in(media.path)
    if found is None:
        raise DateError(
            f"its name does not match the pattern {names.pattern!r}", retryable=False
        )
    return found


def _skip(app_ctx: AppContext, media: MediaFile, why: str) -> None:
    """Report a file left alone, and why."""
    app_ctx.console.print(f"[dim]Skipped {media.path}: {why}[/dim]", highlight=False)


def _local(when: datetime) -> str:
    """Return ``when`` as local wall-clock time, for display.

    Container dates are UTC and EXIF dates are naive wall-clock time; showing
    the first as UTC would make one file look an hour off from the other.
    """
    shown = when.astimezone() if when.tzinfo is not None else when
    return f"{shown:%Y-%m-%d %H:%M}"


def _uploaded_at(ledger: Ledger, path: Path) -> str | None:
    """Return when the ledger says this exact content was uploaded, if ever.

    The stat cache answers for a path an upload run has already seen;
    anything else is hashed, so a file uploaded from another path or after a
    rename is still recognised.

    Raises:
        OSError: The file could not be read.
    """
    stat = path.stat()
    digest = ledger.cached_hash(path, stat.st_size, stat.st_mtime_ns)
    record = ledger.lookup(digest or hash_file(path))
    return None if record is None else f"{record.uploaded_at:%Y-%m-%d}"
