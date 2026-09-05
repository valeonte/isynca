from datetime import UTC, datetime
from pathlib import Path

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger, UploadStatus


def seed(data_dir, **kwargs):
    with Ledger(data_dir / "ledger.db") as ledger:
        ledger.record_upload(**kwargs)


def test_stats_on_empty_ledger(invoke):
    result = invoke("ledger", "stats")
    assert result.exit_code == 0
    assert "Total uploads" in result.output


def test_stats_counts_records(invoke, data_dir):
    seed(
        data_dir,
        content_hash="abc",
        size=2048,
        path=Path("/videos/a.mp4"),
        status=UploadStatus.CONFIRMED,
    )
    result = invoke("ledger", "stats")
    assert "2.0 KiB" in result.output


def test_list_on_empty_ledger(invoke):
    result = invoke("ledger", "list")
    assert result.exit_code == 0
    assert "empty" in result.output


def test_list_shows_records(invoke, data_dir):
    seed(
        data_dir,
        content_hash="abc",
        size=10,
        path=Path("/videos/holiday.mp4"),
        status=UploadStatus.CONFIRMED,
        uploaded_at=datetime(2026, 3, 4, 5, 6, tzinfo=UTC),
    )
    result = invoke("ledger", "list")
    assert "holiday.mp4" in result.output
    assert "2026-03-04 05:06" in result.output
    assert "confirmed" in result.output


def test_list_respects_the_limit(invoke, data_dir):
    with Ledger(data_dir / "ledger.db") as ledger:
        for index in range(5):
            ledger.record_upload(
                content_hash=f"h{index}",
                size=1,
                path=Path(f"/clip{index}.mp4"),
                status=UploadStatus.CONFIRMED,
                uploaded_at=datetime(2026, 1, index + 1, tzinfo=UTC),
            )
    result = invoke("ledger", "list", "--limit", "2")
    assert "clip4.mp4" in result.output
    assert "clip0.mp4" not in result.output


def test_forget_by_cached_path(invoke, data_dir, tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"bytes")
    with Ledger(data_dir / "ledger.db") as ledger:
        digest = hash_file(target)
        ledger.remember_file(
            target, target.stat().st_size, target.stat().st_mtime_ns, digest
        )
        ledger.record_upload(
            content_hash=digest,
            size=5,
            path=target,
            status=UploadStatus.CONFIRMED,
        )

    result = invoke("ledger", "forget", str(target))
    assert result.exit_code == 0
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.lookup(digest) is None


def test_forget_falls_back_to_hashing_the_file(invoke, data_dir, tmp_path):
    """A file recorded under a different path is still found by its content."""
    target = tmp_path / "moved.mp4"
    target.write_bytes(b"bytes")
    digest = hash_file(target)
    seed(
        data_dir,
        content_hash=digest,
        size=5,
        path=Path("/old/location.mp4"),
        status=UploadStatus.CONFIRMED,
    )

    result = invoke("ledger", "forget", str(target))
    assert result.exit_code == 0
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.lookup(digest) is None


def test_forget_unknown_path_exits_nonzero(invoke, tmp_path):
    result = invoke("ledger", "forget", str(tmp_path / "unknown.mp4"))
    assert result.exit_code == 1
    assert "No ledger entry" in result.output


def test_forget_existing_file_not_in_ledger_exits_nonzero(invoke, tmp_path):
    target = tmp_path / "untracked.mp4"
    target.write_bytes(b"nope")
    assert invoke("ledger", "forget", str(target)).exit_code == 1


def test_prune_removes_stale_rows(invoke, data_dir, tmp_path):
    with Ledger(data_dir / "ledger.db") as ledger:
        ledger.remember_file(tmp_path / "gone.mp4", 1, 2, "h")

    result = invoke("ledger", "prune")
    assert result.exit_code == 0
    assert "Pruned 1" in result.output
