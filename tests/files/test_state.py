from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from isynca.errors import SyncStateError
from isynca.files.state import EntryKind, SyncRecord, SyncState


@pytest.fixture
def state(tmp_path):
    with SyncState(tmp_path / "drive.db") as store:
        yield store


def row(path="a.txt", **overrides):
    base = SyncRecord(
        path=PurePosixPath(path),
        kind=EntryKind.FILE,
        size=10,
        mtime_ns=123,
        content_hash="h1",
        etag="e1",
        remote_size=10,
        remote_modified="2026-01-02T03:04:05+00:00",
    )
    return replace(base, **overrides)


def test_remembers_and_returns_a_record(state, tmp_path):
    state.remember(tmp_path, row())
    stored = state.records(tmp_path)[PurePosixPath("a.txt")]
    assert stored.content_hash == "h1"
    assert stored.etag == "e1"
    assert stored.remote_size == 10
    assert not stored.is_dir


def test_remember_is_an_upsert(state, tmp_path):
    state.remember(tmp_path, row())
    state.remember(tmp_path, row(etag="e2", content_hash="h2"))
    records = state.records(tmp_path)
    assert len(records) == 1
    assert records[PurePosixPath("a.txt")].etag == "e2"


def test_folder_records_round_trip(state, tmp_path):
    state.remember(tmp_path, row("Notes", kind=EntryKind.FOLDER))
    assert state.records(tmp_path)[PurePosixPath("Notes")].is_dir


def test_forget_drops_one_row(state, tmp_path):
    state.remember(tmp_path, row())
    state.forget(tmp_path, PurePosixPath("a.txt"))
    assert state.records(tmp_path) == {}


def test_roots_are_isolated_from_each_other(state, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    state.remember(one, row())
    assert state.records(two) == {}
    assert len(state.records(one)) == 1


def test_a_root_is_identified_by_its_resolved_path(state, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    state.remember(real, row())

    # Reached by another name, it must still be the root already being tracked
    # -- otherwise the next run would look like a first run and adopt everything.
    assert len(state.records(link)) == 1


def test_roots_lists_what_is_tracked(state, tmp_path):
    state.remember(tmp_path, row())
    assert state.roots() == [str(tmp_path.resolve())]


def test_synced_at_defaults_to_now(state, tmp_path):
    state.remember(tmp_path, row())
    stored = state.records(tmp_path)[PurePosixPath("a.txt")]
    assert (datetime.now(UTC) - stored.synced_at).total_seconds() < 60


def test_explicit_synced_at_is_kept(state, tmp_path):
    when = datetime(2020, 5, 6, tzinfo=UTC)
    state.remember(tmp_path, row(synced_at=when))
    assert state.records(tmp_path)[PurePosixPath("a.txt")].synced_at == when


def test_an_unopenable_database_is_fatal(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    with pytest.raises(SyncStateError, match="Could not open sync state"):
        SyncState(blocked / "drive.db")


def test_a_broken_connection_is_reported(state, tmp_path):
    state.close()
    with pytest.raises(SyncStateError, match="lookup failed"):
        state.records(tmp_path)


def test_a_broken_connection_is_reported_on_write(state, tmp_path):
    state.close()
    with pytest.raises(SyncStateError, match="write failed"):
        state.remember(tmp_path, row())


def test_schema_failure_is_reported(tmp_path, monkeypatch):
    """A connection that cannot run DDL surfaces as an error, not a crash."""
    real_connect = sqlite3.connect

    def closed_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.close()
        return conn

    monkeypatch.setattr(sqlite3, "connect", closed_connect)
    with pytest.raises(SyncStateError, match="Could not initialise"):
        SyncState(tmp_path / "drive.db")


def test_in_memory_state_needs_no_directory():
    with SyncState(Path(":memory:")) as store:
        assert store.roots() == []
