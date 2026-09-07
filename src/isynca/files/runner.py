"""Executing a sync plan.

Work is done in phases rather than in plan order, because the order actions
are decided in is not an order they can be performed in: a file cannot be
uploaded into a folder that does not exist yet, and a folder cannot be
removed until the files inside it are gone. Folders are therefore created
shallowest first, then content moves, then deletions run deepest first.

State is written as each action succeeds. An interrupted run leaves behind an
accurate record of exactly what it got through, so the next run resumes
rather than re-deciding from scratch -- which for a sync means it does not
mistake half-finished work for somebody's edits.

One consequence of iCloud's upload protocol shows up here. There is no
in-place overwrite: uploading over an existing name produces "notes 2.md"
rather than a new version, so updating a remote file means trashing it and
uploading a replacement. The old version stays recoverable in Recently
Deleted, so a failure between the two steps costs a trip to the trash rather
than the file.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from isynca.errors import ConfigError, DriveListingError, ItemError
from isynca.files.client import DriveClient
from isynca.files.local import LocalFile
from isynca.files.planner import ActionKind, SyncAction, SyncPlan
from isynca.files.report import SyncReport
from isynca.files.state import EntryKind, SyncRecord, SyncState
from isynca.files.types import RemoteNode
from isynca.ledger.hashing import hash_file
from isynca.logging import get_logger
from isynca.sync.runner import RetryPolicy

LOGGER = get_logger("files-runner")

ProgressHook = Callable[[SyncAction], None]

DEFAULT_MAX_DELETES = 50
"""How many removals a run will make before insisting on confirmation.

The number is a rail, not a judgement about your files. A sync pointed at the
wrong folder looks exactly like a sync of a folder you emptied on purpose,
and the first one should not be able to clear an account unattended.
"""


def check_deletion_threshold(plan: SyncPlan, limit: int, force: bool = False) -> None:
    """Stop a run that would delete more than ``limit`` things.

    Checked against the whole plan before any of it runs, so a run that trips
    the rail changes nothing at all rather than stopping halfway.

    Raises:
        ConfigError: The plan exceeds the limit and ``force`` was not given.
    """
    deletions = plan.deletions
    if force or limit <= 0 or len(deletions) <= limit:
        return

    sample = ", ".join(str(action.path) for action in deletions[:5])
    raise ConfigError(
        f"This run would delete {len(deletions)} item(s), over the limit of "
        f"{limit} (for example: {sample}). Check the folder is the one you "
        f"meant, then re-run with --force to proceed."
    )


class SyncRunner:
    """Carries out a :class:`SyncPlan` against iCloud and the local disk."""

    def __init__(
        self,
        *,
        client: DriveClient,
        state: SyncState,
        root: Path,
        dry_run: bool = False,
        retry: RetryPolicy | None = None,
        progress: ProgressHook | None = None,
        sleep: Callable[[float], None] = time.sleep,
        hasher: Callable[[Path], str] = hash_file,
    ) -> None:
        self._client = client
        self._state = state
        self._root = root.expanduser()
        self._dry_run = dry_run
        self._retry = retry or RetryPolicy()
        self._progress = progress
        self._sleep = sleep
        self._hasher = hasher
        self._folders: dict[PurePosixPath, RemoteNode] = {}
        self._uploaded: dict[PurePosixPath, LocalFile] = {}

    def run(
        self, plan: SyncPlan, remote: dict[PurePosixPath, RemoteNode]
    ) -> SyncReport:
        """Execute ``plan`` and return a report of what happened."""
        report = SyncReport(dry_run=self._dry_run)
        report.unchanged = plan.unchanged
        report.adopted = len(plan.adopted)
        report.suppressed = plan.suppressed

        self._folders = {path: node for path, node in remote.items() if node.is_dir}
        self._folders[PurePosixPath(".")] = self._root_node()
        self._uploaded = {}

        for action in plan.conflicts:
            LOGGER.warning("Conflict at %s: %s", action.path, action.detail)
            report.record_conflict(action)

        for action in self._ordered(plan):
            self._perform(action, report)

        self._refresh_uploads(report)
        self._settle(plan)
        return report

    def _root_node(self) -> RemoteNode:
        """Return the Drive root, or a placeholder when nothing needs it."""
        return self._client.root()

    @staticmethod
    def _ordered(plan: SyncPlan) -> list[SyncAction]:
        """Return the plan's actions in an order they can actually run in."""
        creates = sorted(
            plan.of(ActionKind.MKDIR_LOCAL, ActionKind.MKDIR_REMOTE),
            key=lambda action: action.depth,
        )
        transfers = plan.transfers
        deletes = sorted(
            plan.of(ActionKind.DELETE_LOCAL, ActionKind.DELETE_REMOTE),
            key=lambda action: action.depth,
            reverse=True,
        )
        removes = sorted(
            plan.of(ActionKind.RMDIR_LOCAL, ActionKind.RMDIR_REMOTE),
            key=lambda action: action.depth,
            reverse=True,
        )
        return [*creates, *transfers, *deletes, *removes]

    def _perform(self, action: SyncAction, report: SyncReport) -> None:
        """Run one action, recording success or failure."""
        if self._dry_run:
            LOGGER.info("Would %s %s", action.kind, action.path)
            report.record(action)
            self._notify(action)
            return

        try:
            self._with_retries(action)
        except (ItemError, OSError) as exc:
            LOGGER.error("Could not %s %s: %s", action.kind, action.path, exc)
            report.record_failure(action.path, str(exc))
            self._notify(action)
            return

        report.record(action)
        self._notify(action)

    def _with_retries(self, action: SyncAction) -> None:
        """Run one action, retrying the failures that tend to clear.

        A non-retryable :class:`~isynca.errors.ItemError` is re-raised at
        once: it concerns this path alone, but iCloud has already given its
        final answer about it.
        """
        for attempt in range(1, self._retry.attempts + 1):
            try:
                self._dispatch(action)
            except ItemError as exc:
                if attempt == self._retry.attempts or not exc.retryable:
                    raise
                delay = self._retry.delay_for(attempt)
                LOGGER.warning(
                    "Attempt %d/%d for %s failed (%s); retrying in %.1fs",
                    attempt,
                    self._retry.attempts,
                    action.path,
                    exc,
                    delay,
                )
                self._sleep(delay)
            else:
                return
        raise AssertionError("unreachable")  # pragma: no cover

    def _dispatch(self, action: SyncAction) -> None:
        """Perform one action for real."""
        handlers: dict[ActionKind, Callable[[SyncAction], None]] = {
            ActionKind.MKDIR_LOCAL: self._mkdir_local,
            ActionKind.MKDIR_REMOTE: self._mkdir_remote,
            ActionKind.UPLOAD: self._upload,
            ActionKind.UPDATE_REMOTE: self._update_remote,
            ActionKind.DOWNLOAD: self._download,
            ActionKind.UPDATE_LOCAL: self._download,
            ActionKind.DELETE_REMOTE: self._delete_remote,
            ActionKind.DELETE_LOCAL: self._delete_local,
            ActionKind.RMDIR_REMOTE: self._rmdir_remote,
            ActionKind.RMDIR_LOCAL: self._rmdir_local,
        }
        handlers[action.kind](action)

    def _absolute(self, path: PurePosixPath) -> Path:
        """Return the local path a relative sync path refers to."""
        return self._root / Path(*path.parts)

    def _parent_of(self, path: PurePosixPath) -> RemoteNode:
        """Return the remote folder a path belongs in.

        Raises:
            ItemError: The folder is not known, which means the run failed to
                create it earlier.
        """
        parent = self._folders.get(path.parent)
        if parent is None:
            raise ItemError(f"No remote folder for {path.parent}", retryable=False)
        return parent

    def _mkdir_local(self, action: SyncAction) -> None:
        """Create one local folder and record it."""
        target = self._absolute(action.path)
        target.mkdir(parents=True, exist_ok=True)
        self._remember_dir(action.path, _require_remote(action).etag)

    def _mkdir_remote(self, action: SyncAction) -> None:
        """Create one remote folder, remembering it for its children."""
        created = self._client.mkdir(self._parent_of(action.path), action.path.name)
        self._folders[action.path] = created
        self._remember_dir(action.path, created.etag)

    def _upload(self, action: SyncAction) -> None:
        """Send a new local file to iCloud."""
        local = _require_local(action)
        self._client.upload(self._parent_of(action.path), local.absolute)
        self._uploaded[action.path] = local

    def _update_remote(self, action: SyncAction) -> None:
        """Replace iCloud's copy with the local one.

        Trash first, upload second. iCloud has no overwrite, and uploading
        onto a name that is still taken produces a second file rather than a
        new version -- so the old copy has to go before the new one arrives.
        """
        self._client.trash(_require_remote(action))
        self._upload(action)

    def _download(self, action: SyncAction) -> None:
        """Fetch iCloud's copy and record the agreed state."""
        remote = _require_remote(action)
        target = self._absolute(action.path)
        self._client.download(remote, target)
        stat = target.stat()
        self._state.remember(
            self._root,
            SyncRecord(
                path=action.path,
                kind=EntryKind.FILE,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                content_hash=self._hasher(target),
                etag=remote.etag,
                remote_size=remote.size or 0,
                remote_modified=remote.modified.isoformat()
                if remote.modified
                else None,
            ),
        )

    def _delete_remote(self, action: SyncAction) -> None:
        """Trash iCloud's copy of a file deleted locally."""
        self._client.trash(_require_remote(action))
        self._state.forget(self._root, action.path)

    def _delete_local(self, action: SyncAction) -> None:
        """Remove the local copy of a file deleted in iCloud."""
        self._absolute(action.path).unlink(missing_ok=True)
        self._state.forget(self._root, action.path)

    def _rmdir_remote(self, action: SyncAction) -> None:
        """Trash a remote folder whose local counterpart is gone."""
        self._client.trash(_require_remote(action))
        self._state.forget(self._root, action.path)

    def _rmdir_local(self, action: SyncAction) -> None:
        """Remove a local folder whose remote counterpart is gone.

        ``rmdir`` rather than a recursive delete, on purpose: a folder that
        still holds something -- a file isynca was told to exclude, say -- is
        left standing rather than erased along with its contents.
        """
        target = self._absolute(action.path)
        try:
            target.rmdir()
        except OSError as exc:
            LOGGER.warning("Leaving %s in place: %s", target, exc)
            return
        self._state.forget(self._root, action.path)

    def _refresh_uploads(self, report: SyncReport) -> None:
        """Record the etags iCloud gave the files this run sent.

        An upload does not report the document it created, and a state row
        without the real etag would look like a remote edit on the next run
        -- which would download the file that was just uploaded. Rather than
        pay a round trip per file, the folders that received uploads are
        re-listed once each at the end.
        """
        if self._dry_run or not self._uploaded:
            return

        for folder in sorted({path.parent for path in self._uploaded}):
            node = self._folders.get(folder)
            if node is None:  # pragma: no cover - created folders are recorded
                continue
            try:
                fresh = {child.path: child for child in self._client.children(node)}
            except DriveListingError as exc:
                # Deliberately not fatal here, though it is everywhere else:
                # the transfers already succeeded, and a missing state row
                # only costs the next run an adoption check.
                LOGGER.warning("Could not confirm uploads in %s: %s", folder, exc)
                continue
            self._record_uploaded(folder, fresh, report)

    def _record_uploaded(
        self,
        folder: PurePosixPath,
        fresh: dict[PurePosixPath, RemoteNode],
        report: SyncReport,
    ) -> None:
        """Write state for every file uploaded into one folder."""
        for path, local in self._uploaded.items():
            if path.parent != folder:
                continue
            node = fresh.get(path)
            if node is None:
                LOGGER.warning("iCloud does not list %s after uploading it", path)
                report.record_failure(path, "uploaded but not listed by iCloud")
                continue
            self._state.remember(
                self._root,
                SyncRecord(
                    path=path,
                    kind=EntryKind.FILE,
                    size=local.size,
                    mtime_ns=local.mtime_ns,
                    content_hash=self._hasher(local.absolute),
                    etag=node.etag,
                    remote_size=node.size or 0,
                    remote_modified=node.modified.isoformat()
                    if node.modified
                    else None,
                ),
            )

    def _remember_dir(self, path: PurePosixPath, etag: str) -> None:
        """Record that both sides now hold one folder."""
        self._state.remember(
            self._root, SyncRecord(path=path, kind=EntryKind.FOLDER, etag=etag)
        )

    def _settle(self, plan: SyncPlan) -> None:
        """Write the rows for matched paths and drop the ones long gone."""
        if self._dry_run:
            return
        for record in plan.adopted:
            self._state.remember(self._root, record)
        for path in plan.stale:
            self._state.forget(self._root, path)

    def _notify(self, action: SyncAction) -> None:
        """Invoke the progress hook, if one was supplied."""
        if self._progress is not None:
            self._progress(action)


def _require_local(action: SyncAction) -> LocalFile:
    """Return the action's local file, which its kind guarantees is there."""
    if action.local is None:  # pragma: no cover - guarded by the planner
        raise ItemError(f"No local file for {action.path}", retryable=False)
    return action.local


def _require_remote(action: SyncAction) -> RemoteNode:
    """Return the action's remote node, which its kind guarantees is there."""
    if action.remote is None:  # pragma: no cover - guarded by the planner
        raise ItemError(f"No remote node for {action.path}", retryable=False)
    return action.remote


def remote_index(nodes: Iterable[RemoteNode]) -> dict[PurePosixPath, RemoteNode]:
    """Index a remote walk by path."""
    return {node.path: node for node in nodes}
