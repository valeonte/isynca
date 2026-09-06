"""Turning a filesystem scan into a concrete list of work.

The planner is where the ledger earns its keep: it decides, per file, whether
the bytes actually need sending. Hashing is the expensive step, so the stat
cache is consulted first and a file whose size and mtime are unchanged reuses
its recorded hash rather than being read again.

It is also where an optional capture-date requirement is enforced, so a file
with no "date taken" is reported and held back before anything is uploaded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger, UploadRecord
from isynca.logging import get_logger
from isynca.media.capture import read_capture_date
from isynca.media.types import MediaFile

LOGGER = get_logger("planner")


class SkipReason(StrEnum):
    """Why a discovered file will not be uploaded."""

    ALREADY_UPLOADED = "already uploaded"
    UNREADABLE = "unreadable"
    MISSING_DATE = "no capture date"


@dataclass(frozen=True, slots=True)
class PlannedUpload:
    """A file that needs uploading, with its content hash resolved."""

    media: MediaFile
    content_hash: str

    @property
    def path(self) -> Path:
        """Return the file's path."""
        return self.media.path

    @property
    def size(self) -> int:
        """Return the file's size in bytes."""
        return self.media.size


@dataclass(frozen=True, slots=True)
class SkippedUpload:
    """A file the planner decided against, and why."""

    media: MediaFile
    reason: SkipReason
    record: UploadRecord | None = None
    detail: str | None = None


@dataclass(slots=True)
class UploadPlan:
    """Everything a run intends to do."""

    pending: list[PlannedUpload] = field(default_factory=list)
    skipped: list[SkippedUpload] = field(default_factory=list)

    @property
    def pending_bytes(self) -> int:
        """Return the total size of the files still to upload."""
        return sum(item.size for item in self.pending)

    @property
    def total(self) -> int:
        """Return the number of files considered."""
        return len(self.pending) + len(self.skipped)


class Planner:
    """Decides which discovered files still need uploading."""

    def __init__(
        self,
        ledger: Ledger,
        require_capture_date: bool = False,
        date_reader: Callable[[MediaFile], datetime | None] = read_capture_date,
    ) -> None:
        self._ledger = ledger
        self._require_capture_date = require_capture_date
        self._date_reader = date_reader

    def content_hash(self, media: MediaFile) -> str:
        """Return ``media``'s content hash, reusing the cache when valid."""
        cached = self._ledger.cached_hash(media.path, media.size, media.mtime_ns)
        if cached is not None:
            return cached

        digest = hash_file(media.path)
        self._ledger.remember_file(media.path, media.size, media.mtime_ns, digest)
        return digest

    def evaluate(self, media: MediaFile) -> PlannedUpload | SkippedUpload:
        """Classify a single file as pending or skipped."""
        try:
            digest = self.content_hash(media)
        except OSError as exc:
            LOGGER.warning("Cannot read %s: %s", media.path, exc)
            return SkippedUpload(
                media=media, reason=SkipReason.UNREADABLE, detail=str(exc)
            )

        record = self._ledger.lookup(digest)
        if record is not None:
            # Checked before the capture date on purpose: iCloud already holds
            # this file, so blocking it now would achieve nothing and would
            # keep an archive run from filing it away.
            return SkippedUpload(
                media=media, reason=SkipReason.ALREADY_UPLOADED, record=record
            )

        if self._require_capture_date and self._date_reader(media) is None:
            LOGGER.warning("No capture date in %s", media.path)
            return SkippedUpload(media=media, reason=SkipReason.MISSING_DATE)

        return PlannedUpload(media=media, content_hash=digest)

    def plan(self, media_files: Iterable[MediaFile]) -> UploadPlan:
        """Build a full :class:`UploadPlan` from discovered media."""
        result = UploadPlan()
        for item in self.iter_plan(media_files):
            if isinstance(item, PlannedUpload):
                result.pending.append(item)
            else:
                result.skipped.append(item)
        return result

    def iter_plan(
        self, media_files: Iterable[MediaFile]
    ) -> Iterator[PlannedUpload | SkippedUpload]:
        """Yield decisions lazily, so a huge tree need not be held in memory."""
        for media in media_files:
            yield self.evaluate(media)
