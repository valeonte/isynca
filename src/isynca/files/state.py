"""What the last successful sync agreed on.

Two directory listings cannot describe a two-way sync. "Present locally,
absent remotely" is either a file you just made or a file someone else just
deleted, and nothing in the two listings says which. The difference is the
*previous* state, so it has to be written down.

Each row is one path both sides once agreed on, with enough of each side's
metadata to answer "has this changed since?" without re-reading the content:
the local size, mtime and content hash, and the remote etag, size and
modification date. Rows are keyed by path rather than by Apple's node ids,
because updating a document replaces it -- iCloud has no in-place overwrite,
so a file's ``docwsid`` changes every time it is edited.

Directories get rows too. Without them a folder deleted on one side could
never be removed from the other, for exactly the reason files could not.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from isynca.errors import SyncStateError
from isynca.logging import get_logger

LOGGER = get_logger("state")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced (
    root            TEXT NOT NULL,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    etag            TEXT NOT NULL,
    remote_size     INTEGER NOT NULL,
    remote_modified TEXT,
    synced_at       TEXT NOT NULL,
    PRIMARY KEY (root, path)
);
"""


class EntryKind(StrEnum):
    """Whether a recorded entry was a file or a folder."""

    FILE = "file"
    FOLDER = "folder"


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """One path as both sides last agreed on it."""

    path: PurePosixPath
    kind: EntryKind
    size: int = 0
    mtime_ns: int = 0
    content_hash: str = ""
    etag: str = ""
    remote_size: int = 0
    remote_modified: str | None = None
    synced_at: datetime | None = None

    @property
    def is_dir(self) -> bool:
        """Return whether this entry was a folder."""
        return self.kind is EntryKind.FOLDER


class SyncState:
    """The sync's memory, usable as a context manager.

    Several local roots can be tracked in one database; every query is scoped
    to the root it was asked about, so syncing two folders never lets one
    conclude the other's files were deleted.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise SyncStateError(f"Could not open sync state at {path}: {exc}") from exc

        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        """Create the schema and record its version."""
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise SyncStateError(f"Could not initialise sync state: {exc}") from exc

    def __enter__(self) -> Self:
        """Return the state store for use in a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying connection."""
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    @contextmanager
    def _guard(self, action: str) -> Iterator[sqlite3.Connection]:
        """Translate sqlite failures into :class:`SyncStateError`."""
        try:
            yield self._conn
        except sqlite3.Error as exc:
            raise SyncStateError(f"Sync state {action} failed: {exc}") from exc

    @staticmethod
    def key(root: Path) -> str:
        """Return the identity a local root is stored under.

        Resolved, so the same folder reached by a symlink or a relative path
        is recognised as the one already being tracked rather than starting
        again from an empty state -- which would look like a first run and
        adopt everything.
        """
        return str(root.expanduser().resolve())

    def records(self, root: Path) -> dict[PurePosixPath, SyncRecord]:
        """Return everything recorded for ``root``, keyed by relative path."""
        with self._guard("lookup") as conn:
            rows = conn.execute(
                "SELECT * FROM synced WHERE root = ?", (self.key(root),)
            ).fetchall()
        return {record.path: record for record in map(_row_to_record, rows)}

    def remember(self, root: Path, record: SyncRecord) -> None:
        """Store or refresh one path's agreed state."""
        when = record.synced_at or datetime.now(UTC)
        with self._guard("write") as conn:
            conn.execute(
                "INSERT INTO synced (root, path, kind, size, mtime_ns, "
                "content_hash, etag, remote_size, remote_modified, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(root, path) DO UPDATE SET "
                "kind=excluded.kind, size=excluded.size, "
                "mtime_ns=excluded.mtime_ns, content_hash=excluded.content_hash, "
                "etag=excluded.etag, remote_size=excluded.remote_size, "
                "remote_modified=excluded.remote_modified, "
                "synced_at=excluded.synced_at",
                (
                    self.key(root),
                    str(record.path),
                    str(record.kind),
                    record.size,
                    record.mtime_ns,
                    record.content_hash,
                    record.etag,
                    record.remote_size,
                    record.remote_modified,
                    when.isoformat(),
                ),
            )

    def forget(self, root: Path, path: PurePosixPath) -> None:
        """Drop one path's record, because it is gone from both sides."""
        with self._guard("write") as conn:
            conn.execute(
                "DELETE FROM synced WHERE root = ? AND path = ?",
                (self.key(root), str(path)),
            )

    def roots(self) -> list[str]:
        """Return every local root this database has state for."""
        with self._guard("lookup") as conn:
            rows = conn.execute(
                "SELECT DISTINCT root FROM synced ORDER BY root"
            ).fetchall()
        return [str(row["root"]) for row in rows]


def _row_to_record(row: sqlite3.Row) -> SyncRecord:
    """Build a :class:`SyncRecord` from a database row."""
    return SyncRecord(
        path=PurePosixPath(row["path"]),
        kind=EntryKind(row["kind"]),
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        content_hash=row["content_hash"],
        etag=row["etag"],
        remote_size=row["remote_size"],
        remote_modified=row["remote_modified"],
        synced_at=datetime.fromisoformat(row["synced_at"]),
    )
