from datetime import UTC, datetime

import pytest

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger, UploadStatus
from isynca.media.rotate import read_rotation
from tests.media.conftest import box, mvhd, trak


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "sideways.mp4"
    path.write_bytes(box(b"ftyp", b"isom") + box(b"moov", mvhd(3_000_000_000) + trak()))
    return path


def seed_upload(data_dir, path, content_hash, cache=False):
    with Ledger(data_dir / "ledger.db") as ledger:
        if cache:
            stat = path.stat()
            ledger.remember_file(path, stat.st_size, stat.st_mtime_ns, content_hash)
        ledger.record_upload(
            content_hash=content_hash,
            size=path.stat().st_size,
            path=path,
            status=UploadStatus.CONFIRMED,
            uploaded_at=datetime(2025, 6, 7, tzinfo=UTC),
        )


def test_rotates_a_video(invoke, video):
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert "sideways_rot90.mp4 (0° → 90°)" in result.output
    assert read_rotation(video) == 0
    assert read_rotation(video.with_name("sideways_rot90.mp4")) == 90


def test_dry_run_reports_without_writing(invoke, video):
    result = invoke("media", "rotate", "--clockwise", "180", "--dry-run", str(video))
    assert result.exit_code == 0, result.output
    assert "Would write" in result.output
    assert "sideways_rot180.mp4 (0° → 180°)" in result.output
    assert not video.with_name("sideways_rot180.mp4").exists()


def test_rejects_a_turn_that_is_not_a_quarter(invoke, video):
    result = invoke("media", "rotate", "--clockwise", "45", str(video))
    assert result.exit_code == 2
    assert "90, 180 or 270" in result.output


def test_a_bad_file_fails_the_run_but_not_the_others(invoke, video, tmp_path):
    avi = tmp_path / "old.avi"
    avi.write_bytes(b"RIFF")
    result = invoke("media", "rotate", "--clockwise", "90", str(avi), str(video))
    assert result.exit_code == 1
    assert "Cannot rotate" in result.output
    assert "not an MP4/QuickTime file" in result.output
    assert video.with_name("sideways_rot90.mp4").exists()


def test_warns_when_the_old_version_is_already_in_icloud(invoke, video, data_dir):
    seed_upload(data_dir, video, hash_file(video))
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert result.exit_code == 0, result.output
    assert "uploaded to iCloud Photos on 2025-06-07" in result.output
    assert "delete the old one" in result.output


def test_the_stat_cache_answers_without_hashing(invoke, video, data_dir, monkeypatch):
    seed_upload(data_dir, video, "cached-hash", cache=True)

    def unexpected(_path):
        raise AssertionError("hashed a file the stat cache already knew")

    monkeypatch.setattr("isynca.cli.media.hash_file", unexpected)
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert "uploaded to iCloud Photos" in result.output


def test_an_unreadable_file_while_hashing_is_reported(invoke, video, monkeypatch):
    def fail(_path):
        raise PermissionError("denied")

    monkeypatch.setattr("isynca.cli.media.hash_file", fail)
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert result.exit_code == 1
    assert "denied" in result.output
    assert not video.with_name("sideways_rot90.mp4").exists()
