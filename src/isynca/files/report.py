"""Result accounting for a sync run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import ClassVar

from isynca.files.planner import ActionKind, SyncAction
from isynca.sync.report import format_bytes


@dataclass(frozen=True, slots=True)
class SyncFailure:
    """One action that could not be completed.

    Separate from :class:`isynca.sync.report.Failure` because these paths are
    Apple's, not this machine's: a sync failure is reported at the relative
    path both sides share, which is a POSIX path whichever side failed.
    """

    path: PurePosixPath
    message: str


@dataclass(slots=True)
class SyncReport:
    """Counts, conflicts and failures accumulated over a sync run."""

    uploaded: int = 0
    updated_remote: int = 0
    downloaded: int = 0
    updated_local: int = 0
    deleted_remote: int = 0
    deleted_local: int = 0
    created_remote_dirs: int = 0
    created_local_dirs: int = 0
    removed_remote_dirs: int = 0
    removed_local_dirs: int = 0
    unchanged: int = 0
    adopted: int = 0
    suppressed: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    dry_run: bool = False
    direction: str = "both"
    conflicts: list[tuple[PurePosixPath, str]] = field(default_factory=list)
    failures: list[SyncFailure] = field(default_factory=list)

    _COUNTERS: ClassVar[dict[ActionKind, str]] = {
        ActionKind.UPLOAD: "uploaded",
        ActionKind.UPDATE_REMOTE: "updated_remote",
        ActionKind.DOWNLOAD: "downloaded",
        ActionKind.UPDATE_LOCAL: "updated_local",
        ActionKind.DELETE_REMOTE: "deleted_remote",
        ActionKind.DELETE_LOCAL: "deleted_local",
        ActionKind.MKDIR_REMOTE: "created_remote_dirs",
        ActionKind.MKDIR_LOCAL: "created_local_dirs",
        ActionKind.RMDIR_REMOTE: "removed_remote_dirs",
        ActionKind.RMDIR_LOCAL: "removed_local_dirs",
    }

    @property
    def changed(self) -> int:
        """Return how many things this run actually altered."""
        return sum(getattr(self, name) for name in self._COUNTERS.values())

    @property
    def failed(self) -> int:
        """Return the number of actions that errored."""
        return len(self.failures)

    @property
    def ok(self) -> bool:
        """Return whether the run finished with nothing outstanding.

        A conflict counts as not-ok. Nothing broke, but something needs a
        person, and a run that exits 0 is a run nobody looks at.
        """
        return not self.failures and not self.conflicts

    def record(self, action: SyncAction) -> None:
        """Count one completed action."""
        name = self._COUNTERS[action.kind]
        setattr(self, name, getattr(self, name) + 1)
        if action.kind in (ActionKind.UPLOAD, ActionKind.UPDATE_REMOTE):
            self.bytes_up += action.local.size if action.local else 0
        elif action.kind in (ActionKind.DOWNLOAD, ActionKind.UPDATE_LOCAL):
            self.bytes_down += (action.remote.size or 0) if action.remote else 0

    def record_conflict(self, action: SyncAction) -> None:
        """Record one path the run refused to touch."""
        self.conflicts.append((action.path, action.detail))

    def record_failure(self, path: PurePosixPath, message: str) -> None:
        """Record one action that could not be completed."""
        self.failures.append(SyncFailure(path=path, message=message))

    def summary_rows(self) -> list[tuple[str, str]]:
        """Return label/value pairs for display.

        Rows that would read zero for a plain one-way run are left out, so
        the table shows what happened rather than a wall of noughts.
        """
        rows = [
            ("Uploaded", str(self.uploaded)),
            ("Updated in iCloud", str(self.updated_remote)),
            ("Downloaded", str(self.downloaded)),
            ("Updated locally", str(self.updated_local)),
            ("Deleted from iCloud", str(self.deleted_remote)),
            ("Deleted locally", str(self.deleted_local)),
            ("Unchanged", str(self.unchanged)),
            ("Conflicts", str(len(self.conflicts))),
            ("Failed", str(self.failed)),
            ("Bytes sent", format_bytes(self.bytes_up)),
            ("Bytes received", format_bytes(self.bytes_down)),
        ]
        folders = (
            self.created_remote_dirs
            + self.created_local_dirs
            + self.removed_remote_dirs
            + self.removed_local_dirs
        )
        if folders:
            rows.extend(
                [
                    ("Folders created (iCloud)", str(self.created_remote_dirs)),
                    ("Folders created (local)", str(self.created_local_dirs)),
                    ("Folders removed (iCloud)", str(self.removed_remote_dirs)),
                    ("Folders removed (local)", str(self.removed_local_dirs)),
                ]
            )
        if self.adopted:
            rows.append(("Matched, now tracked", str(self.adopted)))
        if self.suppressed:
            rows.append((f"Ignored ({self.direction} only)", str(self.suppressed)))
        if self.dry_run:
            rows.insert(0, ("Mode", "dry run - nothing was changed"))
        return rows
