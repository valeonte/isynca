import os
from pathlib import Path

import pytest

from isynca.errors import ConfigError
from isynca.media.scanner import Scanner
from isynca.media.types import MediaKind


def names(media_files):
    return sorted(m.path.name for m in media_files)


def test_finds_video_recursively(tree):
    scanner = Scanner()
    assert names(scanner.scan([tree])) == ["a.mp4", "b.mov"]


def test_ignores_audio_and_images_by_default(tree):
    scanner = Scanner()
    found = names(scanner.scan([tree]))
    assert "song.mp3" not in found
    assert "photo.jpg" not in found


def test_include_images(tree):
    scanner = Scanner(kinds=frozenset({MediaKind.VIDEO, MediaKind.IMAGE}))
    assert names(scanner.scan([tree])) == ["a.mp4", "b.mov", "photo.jpg"]


def test_stats_are_recorded(tree):
    scanner = Scanner()
    list(scanner.scan([tree]))
    assert scanner.stats.matched == 2
    assert scanner.stats.files_seen == 5
    assert scanner.stats.skipped_extension == 3
    assert scanner.stats.skipped == 3


def test_min_size_filter(tree):
    scanner = Scanner(min_size=150)
    assert names(scanner.scan([tree])) == ["b.mov"]
    assert scanner.stats.skipped_too_small == 1


def test_exclude_by_basename_glob(tree):
    scanner = Scanner(exclude=["*.mov"])
    assert names(scanner.scan([tree])) == ["a.mp4"]
    assert scanner.stats.skipped_excluded == 1


def test_exclude_by_path_glob(tree):
    scanner = Scanner(exclude=["*/day1/*"])
    assert names(scanner.scan([tree])) == ["a.mp4"]


def test_exclude_prunes_directories(tree):
    scanner = Scanner(exclude=["day1"])
    assert names(scanner.scan([tree])) == ["a.mp4"]
    # The pruned directory's contents were never even stat'd.
    assert scanner.stats.files_seen == 4


def test_single_file_source(tree):
    scanner = Scanner()
    assert names(scanner.scan([tree / "trip" / "a.mp4"])) == ["a.mp4"]


def test_single_non_media_file_source(tree):
    scanner = Scanner()
    assert list(scanner.scan([tree / "docs" / "notes.txt"])) == []


def test_missing_source_raises(tmp_path):
    scanner = Scanner()
    with pytest.raises(ConfigError, match="does not exist"):
        list(scanner.scan([tmp_path / "nope"]))


def test_no_kinds_raises():
    with pytest.raises(ConfigError, match="At least one media kind"):
        Scanner(kinds=frozenset())


def test_overlapping_sources_yield_each_file_once(tree):
    scanner = Scanner()
    found = names(scanner.scan([tree, tree / "trip"]))
    assert found == ["a.mp4", "b.mov"]


def test_unreadable_file_is_recorded(tree, monkeypatch):
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "a.mp4":
            raise OSError("permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    scanner = Scanner()
    assert names(scanner.scan([tree])) == ["b.mov"]
    assert [p.name for p in scanner.stats.unreadable] == ["a.mp4"]


def test_unreadable_directory_is_recorded(tree, monkeypatch):
    def broken_walk(*args, **kwargs):
        kwargs["onerror"](OSError(13, "denied", str(tree / "trip")))
        return iter(())

    monkeypatch.setattr(os, "walk", broken_walk)
    scanner = Scanner()
    assert list(scanner.scan([tree])) == []
    assert scanner.stats.unreadable


def test_symlinked_directory_skipped_by_default(tmp_path, tree):
    link_root = tmp_path / "links"
    link_root.mkdir()
    (link_root / "trip").symlink_to(tree / "trip", target_is_directory=True)
    assert names(Scanner().scan([link_root])) == []


def test_symlinked_directory_followed_when_requested(tmp_path, tree):
    link_root = tmp_path / "links"
    link_root.mkdir()
    (link_root / "trip").symlink_to(tree / "trip", target_is_directory=True)
    scanner = Scanner(follow_symlinks=True)
    assert names(scanner.scan([link_root])) == ["a.mp4", "b.mov"]
