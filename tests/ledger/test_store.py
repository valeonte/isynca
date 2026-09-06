import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from isynca.errors import LedgerError
from isynca.ledger.store import SCHEMA_VERSION, Ledger, UploadStatus


def record(ledger, digest="hash1", size=10, path=Path("/tmp/a.mp4"), **kwargs):
    return ledger.record_upload(
        content_hash=digest,
        size=size,
        path=path,
        status=UploadStatus.CONFIRMED,
        **kwargs,
    )


def test_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "ledger.db"
    with Ledger(target):
        pass
    assert target.exists()


def test_schema_version_is_stamped(ledger):
    version = ledger._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_reopening_is_idempotent(data_dir):
    path = data_dir / "ledger.db"
    with Ledger(path) as first:
        record(first)
    with Ledger(path) as second:
        assert second.lookup("hash1") is not None


def test_lookup_missing_returns_none(ledger):
    assert ledger.lookup("nope") is None


def test_record_and_lookup_roundtrip(ledger):
    when = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    stored = ledger.record_upload(
        content_hash="abc",
        size=42,
        path=Path("/videos/a.mp4"),
        status=UploadStatus.CONFIRMED,
        master_id="m1",
        asset_id="a1",
        uploaded_at=when,
    )
    fetched = ledger.lookup("abc")
    assert fetched == stored
    assert fetched.master_id == "m1"
    assert fetched.asset_id == "a1"
    assert fetched.uploaded_at == when
    assert fetched.status is UploadStatus.CONFIRMED
    assert fetched.first_path == Path("/videos/a.mp4")


def test_record_upload_defaults_timestamp(ledger):
    before = datetime.now(UTC)
    stored = record(ledger)
    assert stored.uploaded_at >= before


def test_record_upload_upserts(ledger):
    record(ledger, digest="abc")
    ledger.record_upload(
        content_hash="abc",
        size=10,
        path=Path("/tmp/a.mp4"),
        status=UploadStatus.DUPLICATE,
        master_id="m2",
    )
    fetched = ledger.lookup("abc")
    assert fetched.status is UploadStatus.DUPLICATE
    assert fetched.master_id == "m2"


def test_cached_hash_returns_none_when_unknown(ledger):
    assert ledger.cached_hash(Path("/a.mp4"), 1, 2) is None


def test_cached_hash_hits_when_stat_unchanged(ledger):
    ledger.remember_file(Path("/a.mp4"), 100, 555, "digest")
    assert ledger.cached_hash(Path("/a.mp4"), 100, 555) == "digest"


@pytest.mark.parametrize(("size", "mtime"), [(101, 555), (100, 556)])
def test_cached_hash_misses_when_stat_changed(ledger, size, mtime):
    ledger.remember_file(Path("/a.mp4"), 100, 555, "digest")
    assert ledger.cached_hash(Path("/a.mp4"), size, mtime) is None


def test_remember_file_updates_existing_row(ledger):
    ledger.remember_file(Path("/a.mp4"), 100, 555, "old")
    ledger.remember_file(Path("/a.mp4"), 200, 666, "new")
    assert ledger.cached_hash(Path("/a.mp4"), 200, 666) == "new"


def test_forget_removes_upload_and_cache(ledger):
    ledger.remember_file(Path("/a.mp4"), 10, 20, "abc")
    record(ledger, digest="abc")
    assert ledger.forget("abc") is True
    assert ledger.lookup("abc") is None
    assert ledger.cached_hash(Path("/a.mp4"), 10, 20) is None


def test_forget_missing_returns_false(ledger):
    assert ledger.forget("nope") is False


def test_forget_path(ledger):
    ledger.remember_file(Path("/a.mp4"), 10, 20, "abc")
    record(ledger, digest="abc")
    assert ledger.forget_path(Path("/a.mp4")) is True
    assert ledger.lookup("abc") is None


def test_forget_path_missing_returns_false(ledger):
    assert ledger.forget_path(Path("/missing.mp4")) is False


def test_prune_drops_only_vanished_files(ledger, tmp_path):
    present = tmp_path / "present.mp4"
    present.write_bytes(b"x")
    ledger.remember_file(present, 1, 2, "h1")
    ledger.remember_file(tmp_path / "gone.mp4", 1, 2, "h2")

    assert ledger.prune() == 1
    assert ledger.cached_hash(present, 1, 2) == "h1"
    assert ledger.stats()["tracked_files"] == 1


def test_stats_counts_by_status(ledger):
    ledger.record_upload(
        content_hash="a", size=10, path=Path("/a"), status=UploadStatus.CONFIRMED
    )
    ledger.record_upload(
        content_hash="b", size=20, path=Path("/b"), status=UploadStatus.UNVERIFIED
    )
    ledger.record_upload(
        content_hash="c", size=30, path=Path("/c"), status=UploadStatus.DUPLICATE
    )
    ledger.remember_file(Path("/a"), 10, 1, "a")

    stats = ledger.stats()
    assert stats["confirmed"] == 1
    assert stats["unverified"] == 1
    assert stats["duplicate"] == 1
    assert stats["total"] == 3
    assert stats["bytes"] == 60
    assert stats["tracked_files"] == 1


def test_stats_on_empty_ledger(ledger):
    assert ledger.stats() == {
        "confirmed": 0,
        "unverified": 0,
        "duplicate": 0,
        "total": 0,
        "bytes": 0,
        "tracked_files": 0,
    }


def test_records_are_newest_first(ledger):
    ledger.record_upload(
        content_hash="old",
        size=1,
        path=Path("/old"),
        status=UploadStatus.CONFIRMED,
        uploaded_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    ledger.record_upload(
        content_hash="new",
        size=1,
        path=Path("/new"),
        status=UploadStatus.CONFIRMED,
        uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert [r.content_hash for r in ledger.records()] == ["new", "old"]


def test_records_empty(ledger):
    assert ledger.records() == []


def test_open_failure_raises_ledger_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(LedgerError, match="Could not open ledger"):
        Ledger(blocker / "ledger.db")


def test_migration_failure_raises_ledger_error(monkeypatch, data_dir):
    """A connection that cannot run DDL surfaces as a LedgerError, not a crash."""
    real_connect = sqlite3.connect

    def closed_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.close()
        return conn

    monkeypatch.setattr(sqlite3, "connect", closed_connect)
    with pytest.raises(LedgerError, match="Could not initialise ledger schema"):
        Ledger(data_dir / "ledger.db")


def test_query_failure_is_wrapped(ledger):
    ledger.close()
    with pytest.raises(LedgerError, match="Ledger lookup failed"):
        ledger.lookup("abc")


def test_write_failure_is_wrapped(ledger):
    ledger.close()
    with pytest.raises(LedgerError, match="Ledger write failed"):
        ledger.remember_file(Path("/a"), 1, 2, "h")


def test_stats_failure_is_wrapped(ledger):
    ledger.close()
    with pytest.raises(LedgerError, match="Ledger lookup failed"):
        ledger.stats()


def test_in_memory_ledger_skips_directory_creation():
    with Ledger(Path(":memory:")) as store:
        record(store)
        assert store.lookup("hash1") is not None
