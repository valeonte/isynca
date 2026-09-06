"""Filing media away into a target folder once iCloud holds it.

Archiving is the one destructive thing isynca does: it removes the local
copy. Two rules keep that safe. A file is only ever moved once iCloud is
*known* to hold it -- a created asset or a reported duplicate, never an
upload that was merely accepted. And a destination that already exists is
never overwritten; the source is left alone and the collision is reported.

Folder structure is reproduced under the target using each file's path
relative to the source root it was discovered under, which is why
:class:`~isynca.media.types.MediaFile` carries that root.

Draining a tree leaves its folders standing, so an archive run ends by
pruning the ones that are now empty -- see :func:`prune_empty_dirs`.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from isynca.errors import ArchiveError, ConfigError
from isynca.ledger.store import UploadStatus
from isynca.logging import get_logger
from isynca.media.types import MediaFile

LOGGER = get_logger("archiver")

ARCHIVABLE_STATUSES: frozenset[UploadStatus] = frozenset(
    {UploadStatus.CONFIRMED, UploadStatus.DUPLICATE}
)
"""Statuses that prove iCloud holds the content.

``UNVERIFIED`` is deliberately absent: the bytes were accepted but CloudKit
had not confirmed the record, and deleting the only local copy on that
promise is not a trade worth making.
"""


class ArchiveOutcome(StrEnum):
    """What happened to one file's local copy."""

    MOVED = "moved"
    COLLISION = "collision"


def is_archivable(status: UploadStatus) -> bool:
    """Return whether ``status`` justifies removing the local copy."""
    return status in ARCHIVABLE_STATUSES


def validate_target(target: Path, sources: Iterable[Path]) -> None:
    """Reject a target that overlaps any source.

    A target nested inside a source would be re-scanned on the next run, and
    a source nested inside the target would have files moved onto themselves.
    Both are caught up front rather than after files have started moving.
    """
    resolved_target = target.expanduser().resolve()
    for source in sources:
        resolved_source = source.expanduser().resolve()
        if resolved_target == resolved_source:
            raise ConfigError(f"Target {target} is the same folder as source {source}")
        if resolved_target.is_relative_to(resolved_source):
            raise ConfigError(f"Target {target} is inside source {source}")
        if resolved_source.is_relative_to(resolved_target):
            raise ConfigError(f"Source {source} is inside target {target}")


class Archiver:
    """Moves archived media under ``target``, preserving folder structure."""

    def __init__(self, target: Path, dry_run: bool = False) -> None:
        self.target = target.expanduser()
        self.dry_run = dry_run
        self.moved: list[Path] = []
        """Every source path this archiver moved, or would have moved.

        A real run leaves the answer on disk, but a dry run does not: without
        this list :func:`prune_empty_dirs` would walk a tree whose files are
        all still present and preview no folders at all.
        """

    def destination_for(self, media: MediaFile) -> Path:
        """Return where ``media`` would land under the target."""
        return self.target / media.relative_path

    def archive(self, media: MediaFile) -> ArchiveOutcome:
        """Move one file under the target and report what happened.

        The collision check runs in a dry run too, so a preview reports the
        same conflicts a real run would hit.

        Raises:
            ArchiveError: The move failed.
        """
        destination = self.destination_for(media)

        if destination.exists():
            LOGGER.warning("Not moving %s: %s already exists", media.path, destination)
            return ArchiveOutcome.COLLISION

        if self.dry_run:
            LOGGER.info("Would move %s -> %s", media.path, destination)
            self.moved.append(media.path)
            return ArchiveOutcome.MOVED

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move falls back to copy-then-delete across filesystems,
            # which os.rename cannot do -- archives often live on other disks.
            shutil.move(str(media.path), str(destination))
        except (OSError, shutil.Error) as exc:
            raise ArchiveError(
                f"Could not move {media.path} to {destination}: {exc}"
            ) from exc

        LOGGER.info("Moved %s -> %s", media.path, destination)
        self.moved.append(media.path)
        return ArchiveOutcome.MOVED


def prune_empty_dirs(
    sources: Iterable[Path],
    dry_run: bool = False,
    moved: Iterable[Path] = (),
) -> list[Path]:
    """Remove the directories left empty under ``sources`` and return them.

    Only directories *below* a source are considered: a root the user named
    on the command line stays put even once it is empty, because deleting the
    folder someone asked to watch is never what they meant.

    Pruning is depth-first, so a branch whose leaves all go takes its parents
    with it. A symlink counts as content -- never something to descend into or
    remove -- and a directory that cannot be listed or removed is logged and
    left behind: a folder outliving its files is untidy, not a failure.

    ``moved`` names the files an archiver has taken away. A real run has left
    that on disk already, but a dry run has not, so passing
    :attr:`Archiver.moved` is what lets a preview count the folders the run
    would empty rather than the none it can see.
    """
    gone = {path.absolute() for path in moved}
    removed: list[Path] = []
    seen: set[Path] = set()
    for source in sources:
        root = source.expanduser()
        # A file source has no structure to prune, and overlapping sources
        # must not be walked twice -- the second pass would find the
        # directories the first one removed.
        if not root.is_dir():
            continue
        key = root.resolve()
        if key in seen:
            continue
        seen.add(key)
        _prune_below(root, dry_run, gone, removed)
    return removed


def _prune_below(
    directory: Path, dry_run: bool, gone: set[Path], removed: list[Path]
) -> bool:
    """Prune inside ``directory`` and report whether it ends up empty.

    The emptiness of a parent is decided from what this pass removed rather
    than by re-reading the directory, so a dry run reports the parents that
    would empty out as well as the leaves.
    """
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        LOGGER.warning("Cannot list %s: %s", directory, exc)
        return False

    empty = True
    for entry in entries:
        if entry.absolute() in gone:
            continue
        if entry.is_symlink() or not entry.is_dir():
            empty = False
            continue
        if not _prune_below(entry, dry_run, gone, removed) or not _remove(
            entry, dry_run
        ):
            empty = False
            continue
        removed.append(entry)
    return empty


def _remove(directory: Path, dry_run: bool) -> bool:
    """Remove one empty directory, reporting whether it went."""
    if dry_run:
        LOGGER.info("Would remove empty folder %s", directory)
        return True
    try:
        directory.rmdir()
    except OSError as exc:
        LOGGER.warning("Could not remove empty folder %s: %s", directory, exc)
        return False
    LOGGER.info("Removed empty folder %s", directory)
    return True
