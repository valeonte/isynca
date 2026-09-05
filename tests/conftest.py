"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from isynca.config import Config
from isynca.ledger.store import Ledger
from isynca.media.types import MediaFile, MediaKind
from tests.fakes.icloud import FakeSession


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Return an isolated data directory."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def ledger(data_dir: Path) -> Iterator[Ledger]:
    """Return an open ledger backed by a temporary file."""
    with Ledger(data_dir / "ledger.db") as store:
        yield store


@pytest.fixture
def config(data_dir: Path) -> Config:
    """Return a config pointing at the temporary data directory."""
    return Config(apple_id="tester@example.com", data_dir=data_dir)


@pytest.fixture
def session() -> FakeSession:
    """Return a fake iCloud session."""
    return FakeSession()


@pytest.fixture
def make_media(tmp_path: Path):
    """Return a factory that writes a file and returns its MediaFile."""

    def factory(
        name: str = "clip.mp4",
        content: bytes = b"video-bytes",
        kind: MediaKind = MediaKind.VIDEO,
    ) -> MediaFile:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stat = path.stat()
        return MediaFile(
            path=path, kind=kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns
        )

    return factory


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Build a small nested media tree and return its root."""
    root = tmp_path / "media"
    (root / "trip" / "day1").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "trip" / "a.mp4").write_bytes(b"a" * 100)
    (root / "trip" / "day1" / "b.mov").write_bytes(b"b" * 200)
    (root / "trip" / "song.mp3").write_bytes(b"c" * 50)
    (root / "trip" / "photo.jpg").write_bytes(b"d" * 60)
    (root / "docs" / "notes.txt").write_text("hello")
    return root
