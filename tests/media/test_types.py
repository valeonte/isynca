from pathlib import Path

import pytest

from isynca.media.types import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MediaFile,
    MediaKind,
    classify,
    extensions_for,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("clip.mp4", MediaKind.VIDEO),
        ("clip.MOV", MediaKind.VIDEO),
        ("photo.jpg", MediaKind.IMAGE),
        ("photo.HEIC", MediaKind.IMAGE),
        ("song.mp3", None),
        ("audio.flac", None),
        ("notes.txt", None),
        ("noextension", None),
    ],
)
def test_classify(name, expected):
    assert classify(Path(name)) is expected


def test_audio_is_not_classifiable():
    """ICloud Photos cannot ingest audio, so no audio extension is listed."""
    audio = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}
    assert not audio & (VIDEO_EXTENSIONS | IMAGE_EXTENSIONS)


def test_extensions_for_single_kind():
    assert extensions_for(frozenset({MediaKind.VIDEO})) == VIDEO_EXTENSIONS


def test_extensions_for_multiple_kinds():
    both = extensions_for(frozenset({MediaKind.VIDEO, MediaKind.IMAGE}))
    assert both == VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


def test_media_file_name(tmp_path):
    media = MediaFile(
        path=tmp_path / "sub" / "clip.mp4", kind=MediaKind.VIDEO, size=1, mtime_ns=2
    )
    assert media.name == "clip.mp4"
