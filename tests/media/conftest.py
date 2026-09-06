"""Builders for media files carrying real, inspectable metadata."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PIL import Image

import isynca.media.capture  # noqa: F401 - registers the HEIF opener

EXIF_IFD_POINTER = 0x8769
DATETIME_ORIGINAL = 0x9003
DATETIME_DIGITIZED = 0x9004
DATETIME = 0x0132


@pytest.fixture
def make_image(tmp_path):
    """Write a real image, optionally stamped with EXIF timestamps."""

    def factory(
        name: str = "photo.jpg",
        original: str | None = None,
        digitized: str | None = None,
        plain: str | None = None,
    ) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (16, 16), (90, 120, 150))

        if original is digitized is plain is None:
            image.save(path)
            return path

        exif = image.getexif()
        if plain is not None:
            exif[DATETIME] = plain
        sub = exif.get_ifd(EXIF_IFD_POINTER)
        if original is not None:
            sub[DATETIME_ORIGINAL] = original
        if digitized is not None:
            sub[DATETIME_DIGITIZED] = digitized
        image.save(path, exif=exif)
        return path

    return factory


def box(box_type: bytes, payload: bytes) -> bytes:
    """Build one ISO-BMFF box."""
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def mvhd(seconds: int, version: int = 0) -> bytes:
    """Build an ``mvhd`` box whose creation time is ``seconds`` since 1904."""
    head = bytes([version, 0, 0, 0])
    if version == 1:
        return box(b"mvhd", head + struct.pack(">QQII", seconds, seconds, 1000, 0))
    return box(b"mvhd", head + struct.pack(">IIII", seconds, seconds, 1000, 0))


def apple_meta(value: str, full_box: bool = True) -> bytes:
    """Build a ``meta`` box holding com.apple.quicktime.creationdate.

    ``full_box`` picks between the ISO layout (a 4-byte version/flags prefix)
    and the QuickTime layout (no prefix), which is the one real difference
    between .mp4 and .mov files here.
    """
    key = b"com.apple.quicktime.creationdate"
    keys = box(
        b"keys",
        b"\0\0\0\0"
        + struct.pack(">I", 1)
        + struct.pack(">I", len(key) + 8)
        + b"mdta"
        + key,
    )
    data = box(b"data", struct.pack(">II", 1, 0) + value.encode())
    ilst = box(b"ilst", box(struct.pack(">I", 1), data))
    body = (b"\0\0\0\0" if full_box else b"") + box(b"hdlr", b"\0" * 24) + keys + ilst
    return box(b"meta", body)


@pytest.fixture
def make_video(tmp_path):
    """Write a minimal but structurally valid MP4/MOV."""

    def factory(name: str = "clip.mp4", moov: bytes | None = None) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = box(b"ftyp", b"isom")
        body += box(b"moov", mvhd(3_000_000_000)) if moov is None else moov
        path.write_bytes(body)
        return path

    return factory
