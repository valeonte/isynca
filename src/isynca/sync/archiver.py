"""Filing media away into a target folder once iCloud holds it.

Archiving is the one destructive thing isynca does: it removes the local
copy. Two rules keep that safe. A file is only ever moved once iCloud is
*known* to hold it -- a created asset or a reported duplicate, never an
upload that was merely accepted. And a destination that already exists is
never overwritten; the source is left alone and the collision is reported.

Folder structure is reproduced under the target using each file's path
relative to the source root it was discovered under, which is why
:class:`~isynca.media.types.MediaFile` carries that root.
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
        return ArchiveOutcome.MOVED
