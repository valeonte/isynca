"""SQLite-backed upload ledger.

Two tables carry the state. ``files`` is a stat cache keyed by path: if a
file's size and mtime are unchanged since the last run, its content hash is
reused instead of re-read. ``uploads`` is the authoritative record, keyed by
content hash, so a file that was renamed or moved is still recognised as
already uploaded.

Upload status is deliberately three-valued. pyicloud returns ``None`` when a
file uploaded successfully but CloudKit had not finished indexing it before
the hydration timeout -- that is not a failure, and re-uploading would waste
the transfer, so it is recorded as ``UNVERIFIED`` and still suppresses a
retry.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from isynca.errors import LedgerError
from isynca.logging import get_logger

LOGGER = get_logger("ledger")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    content_hash TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    first_path   TEXT NOT NULL,
    master_id    TEXT,
    asset_id     TEXT,
    uploaded_at  TEXT NOT NULL,
    status       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files (content_hash);
"""


class UploadStatus(StrEnum):
    """Outcome recorded for an uploaded file."""

    CONFIRMED = "confirmed"
    """iCloud returned the created asset."""

    UNVERIFIED = "unverified"
    """Bytes were accepted but indexing had not completed in time."""

    DUPLICATE = "duplicate"
    """iCloud reported it already held this content."""


@dataclass(frozen=True, slots=True)
class UploadRecord:
    """One row of the ``uploads`` table."""

    content_hash: str
    size: int
    first_path: Path
    master_id: str | None
    asset_id: str | None
    uploaded_at: datetime
    status: UploadStatus


def _row_to_record(row: sqlite3.Row) -> UploadRecord:
    """Build an :class:`UploadRecord` from a database row."""
    return UploadRecord(
        content_hash=row["content_hash"],
        size=row["size"],
        first_path=Path(row["first_path"]),
        master_id=row["master_id"],
        asset_id=row["asset_id"],
        uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
        status=UploadStatus(row["status"]),
    )


class Ledger:
    """Persistent record of uploaded content, usable as a context manager."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread is off because the runner may upload from a
            # small thread pool; every statement below runs under _lock, so
            # the connection is still only touched by one thread at a time.
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False
            )
        except (OSError, sqlite3.Error) as exc:
            raise LedgerError(f"Could not open ledger at {path}: {exc}") from exc

        self._lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        """Create the schema and record its version."""
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise LedgerError(f"Could not initialise ledger schema: {exc}") from exc

    def __enter__(self) -> Self:
        """Return the ledger for use in a ``with`` block."""
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
        """Serialise access and translate sqlite failures into ledger errors."""
        with self._lock:
            try:
                yield self._conn
            except sqlite3.Error as exc:
                raise LedgerError(f"Ledger {action} failed: {exc}") from exc

    def cached_hash(self, path: Path, size: int, mtime_ns: int) -> str | None:
        """Return the stored hash for ``path`` if its stat data is unchanged."""
        with self._guard("lookup") as conn:
            row = conn.execute(
                "SELECT size, mtime_ns, content_hash FROM files WHERE path = ?",
                (str(path),),
            ).fetchone()
        if row is None or row["size"] != size or row["mtime_ns"] != mtime_ns:
            return None
        return str(row["content_hash"])

    def remember_file(
        self, path: Path, size: int, mtime_ns: int, content_hash: str
    ) -> None:
        """Store or refresh the stat cache entry for ``path``."""
        with self._guard("write") as conn:
            conn.execute(
                "INSERT INTO files (path, size, mtime_ns, content_hash) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "size=excluded.size, mtime_ns=excluded.mtime_ns, "
                "content_hash=excluded.content_hash",
                (str(path), size, mtime_ns, content_hash),
            )

    def lookup(self, content_hash: str) -> UploadRecord | None:
        """Return the upload record for ``content_hash``, if any."""
        with self._guard("lookup") as conn:
            row = conn.execute(
                "SELECT * FROM uploads WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def record_upload(
        self,
        *,
        content_hash: str,
        size: int,
        path: Path,
        status: UploadStatus,
        master_id: str | None = None,
        asset_id: str | None = None,
        uploaded_at: datetime | None = None,
    ) -> UploadRecord:
        """Record a completed upload and return the stored row.

        Writing happens immediately after each file rather than in a batch at
        the end, so an interrupted run resumes without re-uploading whatever
        already made it across.
        """
        when = uploaded_at or datetime.now(UTC)
        with self._guard("write") as conn:
            conn.execute(
                "INSERT INTO uploads (content_hash, size, first_path, master_id, "
                "asset_id, uploaded_at, status) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(content_hash) DO UPDATE SET master_id=excluded.master_id, "
                "asset_id=excluded.asset_id, uploaded_at=excluded.uploaded_at, "
                "status=excluded.status",
                (
                    content_hash,
                    size,
                    str(path),
                    master_id,
                    asset_id,
                    when.isoformat(),
                    str(status),
                ),
            )
        return UploadRecord(
            content_hash=content_hash,
            size=size,
            first_path=path,
            master_id=master_id,
            asset_id=asset_id,
            uploaded_at=when,
            status=status,
        )

    def forget(self, content_hash: str) -> bool:
        """Drop the upload record for ``content_hash``; return whether it existed."""
        with self._guard("write") as conn:
            cursor = conn.execute(
                "DELETE FROM uploads WHERE content_hash = ?", (content_hash,)
            )
            conn.execute("DELETE FROM files WHERE content_hash = ?", (content_hash,))
        return cursor.rowcount > 0

    def forget_path(self, path: Path) -> bool:
        """Drop everything recorded for ``path``; return whether it existed."""
        with self._guard("lookup") as conn:
            row = conn.execute(
                "SELECT content_hash FROM files WHERE path = ?", (str(path),)
            ).fetchone()
        if row is None:
            return False
        return self.forget(str(row["content_hash"]))

    def prune(self) -> int:
        """Delete stat-cache rows whose file no longer exists; return the count."""
        with self._guard("lookup") as conn:
            rows = conn.execute("SELECT path FROM files").fetchall()
        missing = [row["path"] for row in rows if not Path(row["path"]).exists()]
        with self._guard("write") as conn:
            conn.executemany(
                "DELETE FROM files WHERE path = ?", [(p,) for p in missing]
            )
        return len(missing)

    def stats(self) -> dict[str, int]:
        """Return row counts and total bytes recorded, keyed for display."""
        with self._guard("lookup") as conn:
            uploads = conn.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes "
                "FROM uploads GROUP BY status"
            ).fetchall()
            tracked = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()

        result = {f"{status}": 0 for status in UploadStatus}
        total_bytes = 0
        for row in uploads:
            result[str(row["status"])] = int(row["n"])
            total_bytes += int(row["bytes"])
        result["total"] = sum(result[f"{status}"] for status in UploadStatus)
        result["bytes"] = total_bytes
        result["tracked_files"] = int(tracked["n"])
        return result

    def records(self) -> list[UploadRecord]:
        """Return every upload record, newest first."""
        with self._guard("lookup") as conn:
            rows = conn.execute(
                "SELECT * FROM uploads ORDER BY uploaded_at DESC"
            ).fetchall()
        return [_row_to_record(row) for row in rows]
