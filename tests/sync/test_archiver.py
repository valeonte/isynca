from pathlib import Path

import pytest

from isynca.errors import ArchiveError, ConfigError
from isynca.ledger.store import UploadStatus
from isynca.sync.archiver import (
    ArchiveOutcome,
    Archiver,
    is_archivable,
    prune_empty_dirs,
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


# --- pruning empty folders ---------------------------------------------------


@pytest.fixture
def drained(tmp_path):
    """A source whose media has been archived away, leaving empty folders."""
    root = tmp_path / "inbox"
    (root / "trip" / "day1").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "notes.txt").write_text("hello")
    return root


def test_prunes_an_empty_branch_and_its_parents(drained):
    removed = prune_empty_dirs([drained])

    assert set(removed) == {drained / "trip" / "day1", drained / "trip"}
    assert not (drained / "trip").exists()
    assert removed.index(drained / "trip" / "day1") < removed.index(drained / "trip")


def test_a_folder_that_still_holds_a_file_stays(drained):
    prune_empty_dirs([drained])
    assert (drained / "docs" / "notes.txt").is_file()


def test_the_source_root_is_never_removed(tmp_path):
    """Deleting the folder someone asked to watch is never what they meant."""
    root = tmp_path / "inbox"
    (root / "trip").mkdir(parents=True)

    removed = prune_empty_dirs([root])

    assert removed == [root / "trip"]
    assert root.is_dir()


def test_dry_run_removes_nothing_but_reports_the_parents_too(drained):
    removed = prune_empty_dirs([drained], dry_run=True)

    assert set(removed) == {drained / "trip" / "day1", drained / "trip"}
    assert (drained / "trip" / "day1").is_dir()


def test_a_dry_run_counts_the_folders_the_moves_would_empty(drained):
    """A preview walks a tree whose files are all still there."""
    clip = drained / "docs" / "clip.mp4"
    clip.write_bytes(b"video-bytes")
    (drained / "docs" / "notes.txt").unlink()

    removed = prune_empty_dirs([drained], dry_run=True, moved=[clip])

    assert drained / "docs" in removed
    assert clip.is_file(), "the preview moved nothing"


def test_pruning_can_never_take_a_file_with_it(drained):
    """``moved`` is a hint; rmdir on a folder that still holds a file refuses."""
    removed = prune_empty_dirs([drained], moved=[drained / "docs" / "notes.txt"])

    assert drained / "docs" not in removed
    assert (drained / "docs" / "notes.txt").is_file()


def test_a_symlink_is_content_not_a_folder_to_prune(drained):
    link = drained / "trip" / "elsewhere"
    link.symlink_to(drained / "docs", target_is_directory=True)

    removed = prune_empty_dirs([drained])

    assert removed == [drained / "trip" / "day1"]
    assert link.is_symlink(), "the link itself must survive"
    assert (drained / "docs" / "notes.txt").is_file(), "and so must its target"


def test_a_file_source_has_nothing_to_prune(drained):
    assert prune_empty_dirs([drained / "docs" / "notes.txt"]) == []
    assert (drained / "trip" / "day1").is_dir()


def test_overlapping_sources_are_walked_once(drained):
    """The second pass would otherwise trip over what the first removed."""
    removed = prune_empty_dirs([drained, drained / "trip", drained])

    assert removed == [drained / "trip" / "day1", drained / "trip"]


def test_home_in_a_source_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "inbox" / "trip").mkdir(parents=True)

    assert prune_empty_dirs([Path("~/inbox")]) == [tmp_path / "inbox" / "trip"]


def test_a_folder_that_cannot_be_removed_is_left_behind(drained, monkeypatch, caplog):
    def boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "rmdir", boom)

    assert prune_empty_dirs([drained]) == []
    assert (drained / "trip" / "day1").is_dir()
    assert "Could not remove empty folder" in caplog.text


def test_a_folder_that_cannot_be_listed_is_left_behind(drained, monkeypatch, caplog):
    real_iterdir = Path.iterdir

    def boom(self):
        if self.name == "day1":
            raise OSError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", boom)

    assert prune_empty_dirs([drained]) == []
    assert (drained / "trip").is_dir(), "an unreadable child keeps its parent"
    assert "Cannot list" in caplog.text
