"""Result accounting for an upload run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from isynca.ledger.store import UploadStatus
from isynca.sync.planner import SkipReason


@dataclass(frozen=True, slots=True)
class Failure:
    """One file that could not be uploaded."""

    path: Path
    message: str


@dataclass(slots=True)
class RunReport:
    """Counts and failures accumulated over an upload run."""

    confirmed: int = 0
    unverified: int = 0
    duplicate: int = 0
    skipped_already: int = 0
    skipped_unreadable: int = 0
    uploaded_bytes: int = 0
    dry_run: bool = False
    failures: list[Failure] = field(default_factory=list)

    @property
    def uploaded(self) -> int:
        """Return the number of files accepted by iCloud this run."""
        return self.confirmed + self.unverified + self.duplicate

    @property
    def skipped(self) -> int:
        """Return the number of files not attempted."""
        return self.skipped_already + self.skipped_unreadable

    @property
    def failed(self) -> int:
        """Return the number of files that errored."""
        return len(self.failures)

    @property
    def ok(self) -> bool:
        """Return whether the run completed without per-file failures."""
        return not self.failures

    def record_status(self, status: UploadStatus, size: int) -> None:
        """Count one successful upload outcome."""
        if status is UploadStatus.CONFIRMED:
            self.confirmed += 1
        elif status is UploadStatus.UNVERIFIED:
            self.unverified += 1
        else:
            self.duplicate += 1
        self.uploaded_bytes += size

    def record_skip(self, reason: SkipReason) -> None:
        """Count one skipped file."""
        if reason is SkipReason.ALREADY_UPLOADED:
            self.skipped_already += 1
        else:
            self.skipped_unreadable += 1

    def record_failure(self, path: Path, message: str) -> None:
        """Record a per-file failure."""
        self.failures.append(Failure(path=path, message=message))

    def summary_rows(self) -> list[tuple[str, str]]:
        """Return label/value pairs for display."""
        rows = [
            ("Uploaded", str(self.confirmed)),
            ("Uploaded (not yet indexed)", str(self.unverified)),
            ("Already in iCloud", str(self.duplicate)),
            ("Skipped (in ledger)", str(self.skipped_already)),
            ("Skipped (unreadable)", str(self.skipped_unreadable)),
            ("Failed", str(self.failed)),
            ("Bytes sent", format_bytes(self.uploaded_bytes)),
        ]
        if self.dry_run:
            rows.insert(0, ("Mode", "dry run - nothing was uploaded"))
        return rows


_UNIT_STEP = 1024.0


def format_bytes(count: int) -> str:
    """Render a byte count in human-readable units."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < _UNIT_STEP or unit == "TiB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= _UNIT_STEP
    raise AssertionError("unreachable")  # pragma: no cover
