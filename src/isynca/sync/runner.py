"""Executing an upload plan.

Uploads run sequentially by default. The pyicloud session wraps a shared
``requests`` session with mutable auth state and carries no documented
thread-safety guarantee, so parallelism is opt-in via ``concurrency``.

Every result is written to the ledger the moment it is known, rather than
batched at the end, so an interrupted run resumes without re-sending whatever
already crossed the wire.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from isynca.errors import FatalError, ItemError
from isynca.icloud.photos import PhotosUploader, UploadOutcome
from isynca.ledger.store import Ledger, UploadStatus
from isynca.logging import get_logger
from isynca.sync.planner import PlannedUpload, UploadPlan
from isynca.sync.report import RunReport

LOGGER = get_logger("runner")

ProgressHook = Callable[[PlannedUpload, UploadStatus | None], None]


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
        concurrency: int = 1,
        retry: RetryPolicy | None = None,
        progress: ProgressHook | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._uploader = uploader
        self._ledger = ledger
        self._dry_run = dry_run
        self._concurrency = max(1, concurrency)
        self._retry = retry or RetryPolicy()
        self._progress = progress
        self._sleep = sleep
        # Guards the report and the progress hook: both are plain mutable
        # state shared across pool threads when concurrency > 1.
        self._lock = threading.Lock()

    def run(self, plan: UploadPlan) -> RunReport:
        """Execute ``plan`` and return a report of what happened."""
        report = RunReport(dry_run=self._dry_run)
        for skipped in plan.skipped:
            report.record_skip(skipped.reason)

        if self._concurrency == 1:
            for item in plan.pending:
                self._process(item, report)
        else:
            self._run_parallel(plan.pending, report)

        return report

    def _run_parallel(self, items: Iterable[PlannedUpload], report: RunReport) -> None:
        """Upload ``items`` across a small thread pool."""
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            list(pool.map(lambda item: self._process(item, report), items))

    def _process(self, item: PlannedUpload, report: RunReport) -> None:
        """Upload one file, record the outcome, and update the report."""
        if self._dry_run:
            LOGGER.info("Would upload %s", item.path)
            self._notify(item, None)
            return

        try:
            outcome = self._upload_with_retries(item)
        except ItemError as exc:
            LOGGER.error("%s", exc)
            with self._lock:
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
        with self._lock:
            report.record_status(outcome.status, item.size)
        LOGGER.info("%s: %s", item.path.name, outcome.status)
        self._notify(item, outcome.status)

    def _upload_with_retries(self, item: PlannedUpload) -> UploadOutcome:
        """Upload with bounded retries, giving up on fatal errors immediately.

        A :class:`FatalError` -- a revoked session, a vanished album -- is
        re-raised untouched, because retrying it would only repeat the same
        failure for every remaining file.
        """
        if self._uploader is None:  # pragma: no cover - guarded by _process
            raise FatalError("No uploader configured for a non-dry run")

        for attempt in range(1, self._retry.attempts + 1):
            try:
                return self._uploader.upload(item.path)
            except FatalError:
                raise
            except ItemError as exc:
                if attempt == self._retry.attempts:
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
            with self._lock:
                self._progress(item, status)


__all__ = ["ProgressHook", "RetryPolicy", "UploadRunner"]
