"""``isynca media`` -- fix up local media files; nothing here talks to iCloud."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from isynca.cli.context import AppContext, get_context
from isynca.errors import RotationError
from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger
from isynca.media.rotate import QUARTER_TURNS, rotate_video

app = typer.Typer(help="Fix up local media files.", no_args_is_help=True)


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
