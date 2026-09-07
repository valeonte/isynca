"""Deciding what a two-way sync should do, from three pictures of the world.

The planner takes the local tree, the remote tree, and what the last run
agreed on, and turns them into a list of actions. It performs no I/O beyond
hashing local files, which is what makes the whole decision table testable
without a network or a filesystem full of fixtures.

The rule that governs everything here: a difference between the two sides is
only actionable once you know *which side moved*. That is what the recorded
state supplies. Without it, "here but not there" could equally mean a new
file or a deleted one, and acting on the wrong reading destroys data.

Where the state cannot say -- both sides changed, or both sides appeared
independently -- the answer is to touch neither and report it. A sync that
guesses is a sync that eventually guesses wrong, and the cost of guessing
wrong is somebody's file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from isynca.files.local import LocalFile, LocalTree
from isynca.files.state import EntryKind, SyncRecord
from isynca.files.types import RemoteNode
from isynca.ledger.hashing import hash_file
from isynca.logging import get_logger

LOGGER = get_logger("files-planner")


class Direction(StrEnum):
    """Which way changes are allowed to flow."""

    BOTH = "both"
    PUSH = "push"
    """Local is authoritative; remote changes are looked at but not applied."""

    PULL = "pull"
    """Remote is authoritative; local changes are looked at but not applied."""


class ActionKind(StrEnum):
    """One thing a sync run intends to do."""

    UPLOAD = "upload"
    UPDATE_REMOTE = "update remote"
    DOWNLOAD = "download"
    UPDATE_LOCAL = "update local"
    DELETE_REMOTE = "delete remote"
    DELETE_LOCAL = "delete local"
    MKDIR_REMOTE = "create remote folder"
    MKDIR_LOCAL = "create local folder"
    RMDIR_REMOTE = "remove remote folder"
    RMDIR_LOCAL = "remove local folder"
    CONFLICT = "conflict"


WRITES_REMOTE: frozenset[ActionKind] = frozenset(
    {
        ActionKind.UPLOAD,
        ActionKind.UPDATE_REMOTE,
        ActionKind.DELETE_REMOTE,
        ActionKind.MKDIR_REMOTE,
        ActionKind.RMDIR_REMOTE,
    }
)

WRITES_LOCAL: frozenset[ActionKind] = frozenset(
    {
        ActionKind.DOWNLOAD,
        ActionKind.UPDATE_LOCAL,
        ActionKind.DELETE_LOCAL,
        ActionKind.MKDIR_LOCAL,
        ActionKind.RMDIR_LOCAL,
    }
)

DELETES: frozenset[ActionKind] = frozenset(
    {
        ActionKind.DELETE_REMOTE,
        ActionKind.DELETE_LOCAL,
        ActionKind.RMDIR_REMOTE,
        ActionKind.RMDIR_LOCAL,
    }
)


@dataclass(frozen=True, slots=True)
class SyncAction:
    """One unit of work, with both sides' view of the path attached."""

    kind: ActionKind
    path: PurePosixPath
    local: LocalFile | None = None
    remote: RemoteNode | None = None
    record: SyncRecord | None = None
    detail: str = ""

    @property
    def depth(self) -> int:
        """Return how deep the path sits, for ordering parents against children."""
        return len(self.path.parts)


@dataclass(slots=True)
class SyncPlan:
    """Everything a run intends to do, plus what it decided against."""

    actions: list[SyncAction] = field(default_factory=list)
    unchanged: int = 0
    adopted: list[SyncRecord] = field(default_factory=list)
    """Paths that matched on both sides with no prior record.

    They need no transfer, but they do need writing to the state store, or
    the next run would make the same first-run judgement all over again.
    """

    stale: list[PurePosixPath] = field(default_factory=list)
    """Recorded paths now gone from both sides; their rows can go."""

    suppressed: int = 0
    """Actions a one-directional run declined to take."""

    def of(self, *kinds: ActionKind) -> list[SyncAction]:
        """Return the planned actions of the given kinds."""
        wanted = frozenset(kinds)
        return [action for action in self.actions if action.kind in wanted]

    @property
    def conflicts(self) -> list[SyncAction]:
        """Return the paths this run refuses to touch."""
        return self.of(ActionKind.CONFLICT)

    @property
    def deletions(self) -> list[SyncAction]:
        """Return every action that removes something, on either side."""
        return [action for action in self.actions if action.kind in DELETES]

    @property
    def transfers(self) -> list[SyncAction]:
        """Return the actions that move bytes."""
        return self.of(
            ActionKind.UPLOAD,
            ActionKind.UPDATE_REMOTE,
            ActionKind.DOWNLOAD,
            ActionKind.UPDATE_LOCAL,
        )

    @property
    def empty(self) -> bool:
        """Return whether there is nothing to do and nothing to report."""
        return not self.actions


class SyncPlanner:
    """Reconciles local, remote, and recorded state into a plan."""

    def __init__(
        self,
        *,
        direction: Direction = Direction.BOTH,
        hasher: Callable[[Path], str] = hash_file,
    ) -> None:
        self._direction = direction
        self._hasher = hasher

    def plan(
        self,
        local: LocalTree,
        remote: dict[PurePosixPath, RemoteNode],
        state: dict[PurePosixPath, SyncRecord],
    ) -> SyncPlan:
        """Decide what to do about every path either side knows of."""
        plan = SyncPlan()
        remote_dirs = {p: n for p, n in remote.items() if n.is_dir}
        remote_files = {p: n for p, n in remote.items() if not n.is_dir}

        for path in sorted(set(local.dirs) | set(remote_dirs) | _dirs_in(state)):
            self._plan_dir(plan, path, local, remote_dirs, state.get(path))

        for path in sorted(set(local.files) | set(remote_files) | _files_in(state)):
            self._plan_file(
                plan,
                path,
                local.files.get(path),
                remote_files.get(path),
                state.get(path),
            )

        _suppress_under_removed_remote_dirs(plan)
        return plan

    def _emit(self, plan: SyncPlan, action: SyncAction) -> None:
        """Add an action to the plan unless the run's direction forbids it."""
        if action.kind in WRITES_REMOTE and self._direction is Direction.PULL:
            plan.suppressed += 1
            return
        if action.kind in WRITES_LOCAL and self._direction is Direction.PUSH:
            plan.suppressed += 1
            return
        plan.actions.append(action)

    def _plan_dir(
        self,
        plan: SyncPlan,
        path: PurePosixPath,
        local: LocalTree,
        remote_dirs: dict[PurePosixPath, RemoteNode],
        record: SyncRecord | None,
    ) -> None:
        """Decide about one folder.

        Folders carry no content, so "changed" does not apply to them: the
        only questions are whether each side has one, and whether the record
        says it used to.
        """
        here = path in local.dirs
        there = remote_dirs.get(path)

        if here and there is not None:
            if record is None:
                plan.adopted.append(
                    SyncRecord(path=path, kind=EntryKind.FOLDER, etag=there.etag)
                )
            else:
                plan.unchanged += 1
            return

        if here and there is None:
            if record is None:
                self._emit(plan, SyncAction(ActionKind.MKDIR_REMOTE, path))
            else:
                self._emit(
                    plan, SyncAction(ActionKind.RMDIR_LOCAL, path, record=record)
                )
            return

        if there is not None:
            if record is None:
                self._emit(plan, SyncAction(ActionKind.MKDIR_LOCAL, path, remote=there))
            else:
                self._emit(
                    plan,
                    SyncAction(
                        ActionKind.RMDIR_REMOTE, path, remote=there, record=record
                    ),
                )
            return

        plan.stale.append(path)

    def _plan_file(
        self,
        plan: SyncPlan,
        path: PurePosixPath,
        local: LocalFile | None,
        remote: RemoteNode | None,
        record: SyncRecord | None,
    ) -> None:
        """Decide about one file, from all three views of it."""
        if local is not None and remote is not None:
            self._plan_both_present(plan, path, local, remote, record)
            return

        if local is not None:
            self._plan_remote_missing(plan, path, local, record)
            return

        if remote is not None:
            self._plan_local_missing(plan, path, remote, record)
            return

        # Neither side holds it, so the only way this path was considered at
        # all is that the state store named it: there is always a row to drop.
        plan.stale.append(path)

    def _plan_both_present(
        self,
        plan: SyncPlan,
        path: PurePosixPath,
        local: LocalFile,
        remote: RemoteNode,
        record: SyncRecord | None,
    ) -> None:
        """Decide about a file both sides hold."""
        if record is None:
            self._plan_first_meeting(plan, path, local, remote)
            return

        here = self._local_changed(local, record)
        there = _remote_changed(remote, record)

        if here and there:
            self._emit(
                plan,
                SyncAction(
                    ActionKind.CONFLICT,
                    path,
                    local=local,
                    remote=remote,
                    record=record,
                    detail="changed on both sides since the last sync",
                ),
            )
        elif here:
            self._emit(
                plan,
                SyncAction(
                    ActionKind.UPDATE_REMOTE,
                    path,
                    local=local,
                    remote=remote,
                    record=record,
                ),
            )
        elif there:
            self._emit(
                plan,
                SyncAction(
                    ActionKind.UPDATE_LOCAL,
                    path,
                    local=local,
                    remote=remote,
                    record=record,
                ),
            )
        else:
            plan.unchanged += 1

    def _plan_first_meeting(
        self,
        plan: SyncPlan,
        path: PurePosixPath,
        local: LocalFile,
        remote: RemoteNode,
    ) -> None:
        """Decide about a file both sides hold with nothing recorded.

        This is the first run against a folder that already mirrors iCloud.
        There is no remote checksum to compare against and downloading every
        file to find out would cost as much as syncing from scratch, so equal
        size is taken as equal content. Unequal size is not guessed at: it
        becomes a conflict, so the worst a wrong adoption can do is leave two
        matching-length files alone.
        """
        if local.size == remote.size:
            plan.adopted.append(
                SyncRecord(
                    path=path,
                    kind=EntryKind.FILE,
                    size=local.size,
                    mtime_ns=local.mtime_ns,
                    content_hash=self._hash(local),
                    etag=remote.etag,
                    remote_size=remote.size or 0,
                    remote_modified=_stamp(remote),
                )
            )
            return

        self._emit(
            plan,
            SyncAction(
                ActionKind.CONFLICT,
                path,
                local=local,
                remote=remote,
                detail="present on both sides, never synced, and different sizes",
            ),
        )

    def _plan_remote_missing(
        self,
        plan: SyncPlan,
        path: PurePosixPath,
        local: LocalFile,
        record: SyncRecord | None,
    ) -> None:
        """Decide about a file only the local side holds."""
        if record is None:
            self._emit(plan, SyncAction(ActionKind.UPLOAD, path, local=local))
            return

        if self._local_changed(local, record):
            self._emit(
                plan,
                SyncAction(
                    ActionKind.CONFLICT,
                    path,
                    local=local,
                    record=record,
                    detail="edited locally but deleted in iCloud",
                ),
            )
            return

        self._emit(
            plan, SyncAction(ActionKind.DELETE_LOCAL, path, local=local, record=record)
        )

    def _plan_local_missing(
        self,
        plan: SyncPlan,
        path: PurePosixPath,
        remote: RemoteNode,
        record: SyncRecord | None,
    ) -> None:
        """Decide about a file only iCloud holds."""
        if record is None:
            self._emit(plan, SyncAction(ActionKind.DOWNLOAD, path, remote=remote))
            return

        if _remote_changed(remote, record):
            self._emit(
                plan,
                SyncAction(
                    ActionKind.CONFLICT,
                    path,
                    remote=remote,
                    record=record,
                    detail="deleted locally but edited in iCloud",
                ),
            )
            return

        self._emit(
            plan,
            SyncAction(ActionKind.DELETE_REMOTE, path, remote=remote, record=record),
        )

    def _local_changed(self, local: LocalFile, record: SyncRecord) -> bool:
        """Return whether a local file's content differs from the record.

        Stat data is checked first because it is free. Only when it differs
        is the file hashed, so a touched-but-unedited file -- which any backup
        tool or editor can produce -- costs one stat rather than one upload.
        """
        if local.size == record.size and local.mtime_ns == record.mtime_ns:
            return False
        return self._hash(local) != record.content_hash

    def _hash(self, local: LocalFile) -> str:
        """Hash one local file, reporting unreadable content as a mismatch."""
        try:
            return self._hasher(local.absolute)
        except OSError as exc:
            LOGGER.warning("Cannot hash %s: %s", local.absolute, exc)
            return ""


def _remote_changed(remote: RemoteNode, record: SyncRecord) -> bool:
    """Return whether iCloud's copy differs from the record.

    Three signals, any of which counts. The etag alone would probably do, but
    "probably" is doing real work in that sentence: isynca's own updates
    always replace the document, so an etag surviving an edit made on another
    device was never something this could observe directly. Size and
    modification date cost nothing to compare and make the question moot.
    """
    return (
        remote.etag != record.etag
        or (remote.size or 0) != record.remote_size
        or _stamp(remote) != record.remote_modified
    )


def _stamp(remote: RemoteNode) -> str | None:
    """Return a remote node's modification time in a comparable form."""
    return remote.modified.isoformat() if remote.modified else None


def _dirs_in(state: dict[PurePosixPath, SyncRecord]) -> set[PurePosixPath]:
    """Return the recorded paths that were folders."""
    return {path for path, record in state.items() if record.is_dir}


def _files_in(state: dict[PurePosixPath, SyncRecord]) -> set[PurePosixPath]:
    """Return the recorded paths that were files."""
    return {path for path, record in state.items() if not record.is_dir}


def _suppress_under_removed_remote_dirs(plan: SyncPlan) -> None:
    """Drop remote deletions already covered by a folder being trashed.

    Trashing a folder in iCloud takes its whole subtree with it, so deleting
    the children first would only produce a run of errors about nodes that
    are already gone. The local side gets no such treatment: local folders
    are removed with ``rmdir`` after their files, precisely so that a folder
    holding something unexpected survives instead of being erased.
    """
    doomed = [a.path for a in plan.actions if a.kind is ActionKind.RMDIR_REMOTE]
    if not doomed:
        return

    def covered(action: SyncAction) -> bool:
        if action.kind not in (ActionKind.DELETE_REMOTE, ActionKind.RMDIR_REMOTE):
            return False
        return any(
            action.path != parent and action.path.is_relative_to(parent)
            for parent in doomed
        )

    plan.actions = [action for action in plan.actions if not covered(action)]
