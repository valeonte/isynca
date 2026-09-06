"""Executing an upload plan.

Uploads run one at a time. The pyicloud session wraps a shared ``requests``
session with mutable auth state and carries no documented thread-safety
guarantee, so there is no parallel path to get wrong.

Every result is written to the ledger the moment it is known, rather than
batched at the end, so an interrupted run resumes without re-sending whatever
already crossed the wire.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from isynca.errors import FatalError, ItemError
from isynca.icloud.photos import PhotosUploader, UploadOutcome
from isynca.ledger.store import Ledger, UploadStatus
from isynca.logging import get_logger
from isynca.media.types import MediaFile
from isynca.sync.archiver import ArchiveOutcome, Archiver, is_archivable
from isynca.sync.planner import (
    PlannedUpload,
    SkippedUpload,
    SkipReason,
    UploadPlan,
)
from isynca.sync.report import RunReport

LOGGER = get_logger("runner")

ProgressHook = Callable[[PlannedUpload, UploadStatus | None], None]
StartHook = Callable[[PlannedUpload], None]
"""Called with each file as its upload begins.

Separate from :data:`ProgressHook`, which fires once the outcome is known: a
progress bar that only heard about finished files would name the file it has
just stopped working on.
"""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often, and how patiently, to retry a failed upload."""

    attempts: int = 3
    initial_delay: float = 1.0
    backoff: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Return the delay in seconds before ``attempt`` (1-based)."""
        return self.initial_delay * (self.backoff ** (attempt - 1))


class UploadRunner:
    """Runs an :class:`UploadPlan` against iCloud, recording results."""

    def __init__(
        self,
        *,
        uploader: PhotosUploader | None,
        ledger: Ledger,
        dry_run: bool = False,
        archiver: Archiver | None = None,
        retry: RetryPolicy | None = None,
        on_start: StartHook | None = None,
        progress: ProgressHook | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._uploader = uploader
        self._ledger = ledger
        self._dry_run = dry_run
        self._archiver = archiver
        self._retry = retry or RetryPolicy()
        self._on_start = on_start
        self._progress = progress
        self._sleep = sleep

    def run(self, plan: UploadPlan) -> RunReport:
        """Execute ``plan`` and return a report of what happened."""
        report = RunReport(dry_run=self._dry_run, archiving=self._archiver is not None)
        for skipped in plan.skipped:
            report.record_skip(skipped.reason)
            self._archive_skipped(skipped, report)

        for item in plan.pending:
            self._process(item, report)

        return report

    def _archive_skipped(self, skipped: SkippedUpload, report: RunReport) -> None:
        """Archive a file the ledger already knows iCloud holds.

        Without this an archive run would never drain its source folder:
        everything uploaded on an earlier run would be skipped here and left
        sitting where it was found.
        """
        if self._archiver is None or skipped.reason is not SkipReason.ALREADY_UPLOADED:
            return
        status = skipped.record.status if skipped.record else None
        self._archive(skipped.media, status, report)

    def _archive(
        self, media: MediaFile, status: UploadStatus | None, report: RunReport
    ) -> None:
        """Move ``media`` into the archive target if iCloud has confirmed it."""
        if self._archiver is None:
            return
        if status is None or not is_archivable(status):
            LOGGER.debug("Holding %s: iCloud has not confirmed it", media.path)
            report.record_held()
            return

        try:
            outcome = self._archiver.archive(media)
        except ItemError as exc:
            LOGGER.error("%s", exc)
            report.record_move_failure(media.path, str(exc))
            return
        report.record_move(outcome is ArchiveOutcome.MOVED)

    def _process(self, item: PlannedUpload, report: RunReport) -> None:
        """Upload one file, record the outcome, and update the report."""
        if self._on_start is not None:
            self._on_start(item)

        if self._dry_run:
            LOGGER.info("Would upload %s", item.path)
            # A preview assumes the upload would succeed, so the archive step
            # it reports is the one a real run would take.
            self._archive(item.media, UploadStatus.CONFIRMED, report)
            self._notify(item, None)
            return

        try:
            outcome = self._upload_with_retries(item)
        except ItemError as exc:
            LOGGER.error("%s", exc)
            report.record_failure(item.path, str(exc))
            self._notify(item, None)
            return

        self._ledger.record_upload(
            content_hash=item.content_hash,
            size=item.size,
            path=item.path,
            status=outcome.status,
            master_id=outcome.master_id,
            asset_id=outcome.asset_id,
        )
        report.record_status(outcome.status, item.size)
        LOGGER.debug("%s: %s", item.path.name, outcome.status)
        self._archive(item.media, outcome.status, report)
        self._notify(item, outcome.status)

    def _upload_with_retries(self, item: PlannedUpload) -> UploadOutcome:
        """Upload with bounded retries, giving up on settled errors immediately.

        A :class:`FatalError` -- a revoked session, a vanished album -- is
        re-raised untouched, because retrying it would only repeat the same
        failure for every remaining file. An :class:`ItemError` marked not
        retryable is re-raised too: it concerns this file alone, but iCloud
        has already given its final answer about it.
        """
        if self._uploader is None:  # pragma: no cover - guarded by _process
            raise FatalError("No uploader configured for a non-dry run")

        for attempt in range(1, self._retry.attempts + 1):
            try:
                return self._uploader.upload(item.path)
            except FatalError:
                raise
            except ItemError as exc:
                if attempt == self._retry.attempts or not exc.retryable:
                    raise
                delay = self._retry.delay_for(attempt)
                LOGGER.warning(
                    "Attempt %d/%d for %s failed (%s); retrying in %.1fs",
                    attempt,
                    self._retry.attempts,
                    item.path.name,
                    exc,
                    delay,
                )
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _notify(self, item: PlannedUpload, status: UploadStatus | None) -> None:
        """Invoke the progress hook, if one was supplied."""
        if self._progress is not None:
            self._progress(item, status)


__all__ = ["ProgressHook", "RetryPolicy", "StartHook", "UploadRunner"]
