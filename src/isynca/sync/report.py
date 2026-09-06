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
    skipped_missing_date: int = 0
    uploaded_bytes: int = 0
    dry_run: bool = False
    archiving: bool = False
    moved: int = 0
    move_collisions: int = 0
    held_in_place: int = 0
    pruning: bool = False
    pruned_dirs: int = 0
    failures: list[Failure] = field(default_factory=list)
    move_failures: list[Failure] = field(default_factory=list)

    @property
    def uploaded(self) -> int:
        """Return the number of files accepted by iCloud this run."""
        return self.confirmed + self.unverified + self.duplicate

    @property
    def skipped(self) -> int:
        """Return the number of files not attempted."""
        return (
            self.skipped_already + self.skipped_unreadable + self.skipped_missing_date
        )

    @property
    def failed(self) -> int:
        """Return the number of files that errored."""
        return len(self.failures) + len(self.move_failures)

    @property
    def ok(self) -> bool:
        """Return whether the run completed without per-file failures."""
        return not self.failures and not self.move_failures

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
        elif reason is SkipReason.MISSING_DATE:
            self.skipped_missing_date += 1
        else:
            self.skipped_unreadable += 1

    def record_failure(self, path: Path, message: str) -> None:
        """Record a per-file upload failure."""
        self.failures.append(Failure(path=path, message=message))

    def record_move(self, moved: bool) -> None:
        """Count one archive attempt as a move or a collision."""
        if moved:
            self.moved += 1
        else:
            self.move_collisions += 1

    def record_held(self) -> None:
        """Count one file left in place because iCloud has not confirmed it."""
        self.held_in_place += 1

    def record_prune(self, count: int) -> None:
        """Count the empty folders removed after archiving."""
        self.pruned_dirs += count

    def record_move_failure(self, path: Path, message: str) -> None:
        """Record a file that could not be moved into the target."""
        self.move_failures.append(Failure(path=path, message=message))

    def all_failures(self) -> list[Failure]:
        """Return upload and move failures together, for display."""
        return [*self.failures, *self.move_failures]

    def summary_rows(self) -> list[tuple[str, str]]:
        """Return label/value pairs for display."""
        rows = [
            ("Uploaded", str(self.confirmed)),
            ("Uploaded (not yet indexed)", str(self.unverified)),
            ("Already in iCloud", str(self.duplicate)),
            ("Skipped (in ledger)", str(self.skipped_already)),
            ("Skipped (unreadable)", str(self.skipped_unreadable)),
            ("Skipped (no capture date)", str(self.skipped_missing_date)),
            ("Failed", str(self.failed)),
            ("Bytes sent", format_bytes(self.uploaded_bytes)),
        ]
        if self.archiving:
            rows.extend(
                [
                    ("Moved to target", str(self.moved)),
                    ("Not moved (already at target)", str(self.move_collisions)),
                    ("Held (iCloud not confirmed)", str(self.held_in_place)),
                ]
            )
        if self.pruning:
            rows.append(("Empty folders removed", str(self.pruned_dirs)))
        if self.dry_run:
            mode = "dry run - nothing was uploaded, moved, or removed"
            rows.insert(0, ("Mode", mode))
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
