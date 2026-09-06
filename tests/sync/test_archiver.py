from pathlib import Path

import pytest

from isynca.errors import ArchiveError, ConfigError
from isynca.ledger.store import UploadStatus
from isynca.sync.archiver import (
    ArchiveOutcome,
    Archiver,
    is_archivable,
    validate_target,
)


@pytest.fixture
def target(tmp_path):
    return tmp_path / "archive"


def test_moves_a_file_and_creates_the_target(target, make_media):
    media = make_media(name="clip.mp4")
    outcome = Archiver(target).archive(media)

    assert outcome is ArchiveOutcome.MOVED
    assert (target / "clip.mp4").read_bytes() == b"video-bytes"
    assert not media.path.exists()


def test_preserves_nested_folder_structure(target, make_media):
    media = make_media(name="trip/day1/clip.mp4")
    Archiver(target).archive(media)

    assert (target / "trip" / "day1" / "clip.mp4").is_file()


def test_destination_for_reports_where_a_file_would_land(target, make_media):
    media = make_media(name="trip/clip.mp4")
    assert Archiver(target).destination_for(media) == target / "trip" / "clip.mp4"


def test_existing_destination_is_never_overwritten(target, make_media):
    media = make_media(name="clip.mp4", content=b"new content")
    (target).mkdir()
    (target / "clip.mp4").write_bytes(b"original")

    outcome = Archiver(target).archive(media)

    assert outcome is ArchiveOutcome.COLLISION
    assert (target / "clip.mp4").read_bytes() == b"original"
    assert media.path.exists(), "the source must survive a collision"


def test_dry_run_moves_nothing(target, make_media):
    media = make_media(name="clip.mp4")
    outcome = Archiver(target, dry_run=True).archive(media)

    assert outcome is ArchiveOutcome.MOVED
    assert media.path.exists()
    assert not target.exists()


def test_dry_run_still_reports_collisions(target, make_media):
    media = make_media(name="clip.mp4")
    target.mkdir()
    (target / "clip.mp4").write_bytes(b"already here")

    assert Archiver(target, dry_run=True).archive(media) is ArchiveOutcome.COLLISION


def test_move_failure_becomes_an_archive_error(target, make_media, monkeypatch):
    media = make_media(name="clip.mp4")

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("isynca.sync.archiver.shutil.move", boom)
    with pytest.raises(ArchiveError, match="Could not move"):
        Archiver(target).archive(media)


def test_target_home_is_expanded(tmp_path, monkeypatch, make_media):
    monkeypatch.setenv("HOME", str(tmp_path))
    media = make_media(name="clip.mp4")
    Archiver(Path("~/archive")).archive(media)
    assert (tmp_path / "archive" / "clip.mp4").is_file()


@pytest.mark.parametrize(
    "status",
    [UploadStatus.CONFIRMED, UploadStatus.DUPLICATE],
)
def test_confirmed_content_is_archivable(status):
    assert is_archivable(status)


def test_unverified_content_is_not_archivable():
    """Bytes accepted is not proof iCloud holds it; keep the local copy."""
    assert not is_archivable(UploadStatus.UNVERIFIED)


def test_validate_target_accepts_a_separate_folder(tmp_path):
    source = tmp_path / "inbox"
    source.mkdir()
    validate_target(tmp_path / "archive", [source])


def test_validate_target_rejects_a_target_inside_a_source(tmp_path):
    source = tmp_path / "inbox"
    source.mkdir()
    with pytest.raises(ConfigError, match="is inside source"):
        validate_target(source / "done", [source])


def test_validate_target_rejects_a_source_inside_the_target(tmp_path):
    target = tmp_path / "archive"
    source = target / "inbox"
    source.mkdir(parents=True)
    with pytest.raises(ConfigError, match="is inside target"):
        validate_target(target, [source])


def test_validate_target_rejects_the_same_folder(tmp_path):
    with pytest.raises(ConfigError, match="same folder"):
        validate_target(tmp_path, [tmp_path])
