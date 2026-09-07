from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from isynca.files.local import DEFAULT_EXCLUDES, ExcludeRules, LocalScanner


@pytest.fixture
def root(tmp_path):
    (tmp_path / "Notes" / "sub").mkdir(parents=True)
    (tmp_path / "top.txt").write_bytes(b"top")
    (tmp_path / "Notes" / "todo.md").write_bytes(b"todo")
    (tmp_path / "Notes" / "sub" / "deep.bin").write_bytes(b"deep")
    return tmp_path


def test_scan_finds_every_file_regardless_of_type(root):
    tree = LocalScanner().scan(root)
    assert sorted(str(p) for p in tree.files) == [
        "Notes/sub/deep.bin",
        "Notes/todo.md",
        "top.txt",
    ]


def test_scan_records_folders(root):
    tree = LocalScanner().scan(root)
    assert sorted(str(p) for p in tree.dirs) == ["Notes", "Notes/sub"]


def test_paths_are_relative_and_posix(root):
    tree = LocalScanner().scan(root)
    entry = tree.files[PurePosixPath("Notes/todo.md")]
    assert entry.absolute == root / "Notes" / "todo.md"
    assert entry.size == 4


def test_bird_scratch_folders_are_excluded_by_default(root):
    """A real account carries ten of these; syncing them would be pure noise."""
    junk = root / ".com-apple-bird-noname-ABC"
    junk.mkdir()
    (junk / "legacy").mkdir()
    tree = LocalScanner().scan(root)
    assert not any(".com-apple" in str(p) for p in tree.dirs)
    assert tree.excluded == 1


def test_ds_store_and_partials_are_excluded_by_default(root):
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "half.txt.isynca-part").write_bytes(b"partial")
    tree = LocalScanner().scan(root)
    assert sorted(str(p) for p in tree.files) == [
        "Notes/sub/deep.bin",
        "Notes/todo.md",
        "top.txt",
    ]
    assert tree.excluded == 2


def test_extra_excludes_stack_on_the_defaults(root):
    tree = LocalScanner(exclude=["*.bin"]).scan(root)
    assert not any(str(p).endswith(".bin") for p in tree.files)
    assert DEFAULT_EXCLUDES[0] in LocalScanner(exclude=["*.bin"]).exclude


def test_path_patterns_match_as_well_as_names(root):
    tree = LocalScanner(exclude=["Notes/*"]).scan(root)
    assert PurePosixPath("Notes/todo.md") not in tree.files


def test_symlinked_files_are_never_synced(root):
    (root / "link.txt").symlink_to(root / "top.txt")
    tree = LocalScanner().scan(root)
    assert PurePosixPath("link.txt") not in tree.files
    assert tree.symlinks == 1


def test_symlinked_folders_are_never_descended(root):
    (root / "linkdir").symlink_to(root / "Notes")
    tree = LocalScanner().scan(root)
    assert PurePosixPath("linkdir") not in tree.dirs
    assert not any(str(p).startswith("linkdir") for p in tree.files)
    assert tree.symlinks == 1


def test_an_unstattable_file_is_reported_not_fatal(root, monkeypatch):
    real = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name == "top.txt":
            raise OSError("permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    tree = LocalScanner().scan(root)
    assert PurePosixPath("top.txt") not in tree.files
    assert tree.unreadable == [root / "top.txt"]


def test_an_unlistable_directory_is_reported_not_fatal(root, monkeypatch):
    def walk(path, followlinks=False, onerror=None):
        assert onerror is not None
        onerror(OSError(13, "denied", str(root / "Notes")))
        return iter(())

    monkeypatch.setattr(os, "walk", walk)
    tree = LocalScanner().scan(root)
    assert tree.unreadable == [root / "Notes"]


def test_exclude_rules_cover_a_whole_subtree():
    """A folder's exclusion must reach its contents, not just its own name."""
    rules = ExcludeRules()
    assert rules.matches(PurePosixPath(".com-apple-bird-noname-ABC"))
    assert rules.matches(PurePosixPath(".com-apple-bird-noname-ABC/legacy/deep.txt"))


def test_exclude_rules_leave_the_root_alone():
    assert not ExcludeRules().matches(PurePosixPath("."))


def test_extra_patterns_join_the_defaults():
    rules = ExcludeRules(["*.tmp"])
    assert rules.matches(PurePosixPath("Notes/scratch.tmp"))
    assert rules.matches(PurePosixPath(".DS_Store"))
    assert not rules.matches(PurePosixPath("Notes/keep.txt"))


def test_a_scanner_accepts_prebuilt_rules(root):
    tree = LocalScanner(exclude=ExcludeRules(["*.bin"])).scan(root)
    assert not any(str(p).endswith(".bin") for p in tree.files)
