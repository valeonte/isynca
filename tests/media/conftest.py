"""Builders for media files carrying real, inspectable metadata."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import ClassVar

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


IDENTITY = (1 << 16, 0, 0, 1 << 16)


def tkhd(
    cells: tuple[int, int, int, int] = IDENTITY,
    width: int = 1280,
    height: int = 720,
    version: int = 0,
) -> bytes:
    """Build a ``tkhd`` box with the given ``(a, b, c, d)`` matrix cells.

    ``width`` and ``height`` are whole pixels; they are stored as 16.16.
    """
    a, b, c, d = cells
    head = bytes([version, 0, 0, 7])
    times = struct.pack(">QQIIQ" if version == 1 else ">IIIII", 1, 1, 1, 0, 1000)
    reserved = b"\0" * 8 + struct.pack(">hhhh", 0, 0, 0, 0)
    matrix = struct.pack(">9i", a, b, 0, c, d, 0, 0, 0, 1 << 30)
    size = struct.pack(">II", width << 16, height << 16)
    return box(b"tkhd", head + times + reserved + matrix + size)


def trak(handler: bytes = b"vide", header: bytes | None = None) -> bytes:
    """Build a ``trak`` holding a track header and a ``handler`` media box."""
    hdlr = box(b"hdlr", b"\0" * 8 + handler + b"\0" * 12)
    mdia = box(b"mdia", hdlr)
    return box(b"trak", (tkhd() if header is None else header) + mdia)


class FakePopen:
    """Stands in for ffmpeg: writes the target and reports progress."""

    instances: ClassVar[list[FakePopen]] = []

    def __init__(self, command, *, lines=(), stderr="", code=0, raise_on=None):
        self.command = command
        self.stdout = (
            iter(lines) if raise_on is None else self._raising(lines, raise_on)
        )
        self.stderr = _Text(stderr)
        self.code = code
        self.killed = False
        Path(command[-1]).write_bytes(b"converted")
        FakePopen.instances.append(self)

    @staticmethod
    def _raising(lines, exc):
        yield from lines
        raise exc

    def wait(self):
        return self.code

    def kill(self):
        self.killed = True

    def __enter__(self):
        """Behave like Popen as a context manager."""
        return self

    def __exit__(self, *exc):
        """Leave exceptions to propagate, as Popen does."""
        return False


class _Text:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text


@pytest.fixture
def ffmpeg(monkeypatch):
    """Replace Popen with a FakePopen configured by keyword arguments."""
    FakePopen.instances = []

    def install(**behaviour):
        monkeypatch.setattr(
            "isynca.media.convert.subprocess.Popen",
            lambda command, **_kw: FakePopen(command, **behaviour),
        )
        return FakePopen.instances

    return install
