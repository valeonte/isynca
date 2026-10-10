"""Turning a filesystem scan into a concrete list of work.

The planner is where the ledger earns its keep: it decides, per file, whether
the bytes actually need sending. Hashing is the expensive step, so the stat
cache is consulted first and a file whose size and mtime are unchanged reuses
its recorded hash rather than being read again.

It is also where an optional capture-date requirement is enforced, so a file
with no "date taken" is reported and held back before anything is uploaded.

A ledger hit does not always mean the file is done. An ``UNVERIFIED`` record
-- bytes accepted, no record names returned -- is planned again once it has
had time to settle. iCloud cannot be asked about content it never named: no
CloudKit query matches on a checksum or a filename. The registration step
can, though, because it de-duplicates on content, so re-sending the file is
the check. It answers ``DUPLICATE`` if the first upload landed, or a fresh
``CONFIRMED`` if it did not, and either one lets an archive run move the file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger, UploadRecord, UploadStatus
from isynca.logging import get_logger
from isynca.media.capture import read_capture_date
from isynca.media.types import MediaFile

LOGGER = get_logger("planner")

RECHECK_AFTER = timedelta(hours=1)
"""How long an unverified upload is left alone before it is re-sent.

Apple may still be ingesting a file for a while after accepting it, and a
second copy sent during that window is not certain to be recognised as the
same content. Indexing normally finishes within 20 seconds, so an hour leaves
a wide margin while still letting the next day's run settle the file.
"""


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
    previous: UploadRecord | None = None
    """The unverified record this upload re-checks, if it is a re-check."""

    @property
    def is_recheck(self) -> bool:
        """Return whether this upload re-sends a file iCloud never confirmed."""
        return self.previous is not None

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
        recheck_after: timedelta = RECHECK_AFTER,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ledger = ledger
        self._require_capture_date = require_capture_date
        self._date_reader = date_reader
        self._recheck_after = recheck_after
        self._clock = clock

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
        if record is not None and not self._due_for_recheck(record):
            # Checked before the capture date on purpose: iCloud already holds
            # this file, so blocking it now would achieve nothing and would
            # keep an archive run from filing it away.
            return SkippedUpload(
                media=media, reason=SkipReason.ALREADY_UPLOADED, record=record
            )

        # A re-check is not exempt: the first upload may never have landed,
        # in which case this one creates the asset, and an undated file must
        # not get in that way when the requirement is on.
        if self._require_capture_date and self._date_reader(media) is None:
            LOGGER.warning("No capture date in %s", media.path)
            return SkippedUpload(media=media, reason=SkipReason.MISSING_DATE)

        if record is not None:
            LOGGER.debug("Re-checking %s: iCloud never confirmed it", media.path)
        return PlannedUpload(media=media, content_hash=digest, previous=record)

    def _due_for_recheck(self, record: UploadRecord) -> bool:
        """Return whether ``record`` is unverified and old enough to re-send."""
        if record.status is not UploadStatus.UNVERIFIED:
            return False
        return self._clock() - record.uploaded_at >= self._recheck_after

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
