"""Writing a date taken into a file that has none, without re-encoding it.

iCloud Photos dates an upload from metadata inside the file. A file without
any lands on the day it was uploaded, which for an old video means it
disappears among this week's photos. The fix is to write a date in.

Two formats can take one losslessly:

* **JPEG.** The date goes into EXIF as ``DateTimeOriginal`` and
  ``DateTimeDigitized``, with ``OffsetTimeOriginal`` so the wall-clock time
  keeps its timezone. Only the EXIF segment is replaced; the compressed image
  data is copied byte for byte.
* **MP4, MOV, M4V, 3GP.** The creation and modification times in the
  ``mvhd``, ``tkhd`` and ``mdhd`` headers are fixed-size fields, so they are
  overwritten in place and nothing else in the file moves.

Anything else is refused. A video that already carries Apple's own
``com.apple.quicktime.creationdate`` is refused too: that key wins over the
headers written here, so changing only the headers would change nothing.

The original is never modified. The dated copy is written beside it as
``<stem>_dated<suffix>`` and keeps the original's modification time.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

from isynca.errors import DateError
from isynca.media.bmff import find_box, iter_boxes
from isynca.media.capture import (
    DATETIME_DIGITIZED,
    DATETIME_ORIGINAL,
    EXIF_DATETIME_FORMAT,
    EXIF_IFD_POINTER,
    QUICKTIME_EPOCH,
    apple_creation_date,
    read_capture_date,
)
from isynca.media.types import MediaFile, MediaKind

DATED_SUFFIX = "_dated"

OFFSET_TIME_ORIGINAL = 0x9011
OFFSET_TIME_DIGITIZED = 0x9012

_JPEG_FORMATS = frozenset({"JPEG", "MPO"})
_SOI = b"\xff\xd8"
_SOS = 0xDA
_APP0 = 0xE0
_APP1 = 0xE1
_EXIF_HEADER = b"Exif\x00\x00"
_MAX_SEGMENT = 0xFFFF

Writer = Callable[[Path], None]
"""Writes the dated version of a file to the path it is given."""


@dataclass(frozen=True, slots=True)
class DateFix:
    """One dated copy: where it came from, where it went, and the dates."""

    source: Path
    output: Path
    before: datetime | None
    after: datetime


def dated_path(path: Path) -> Path:
    """Return where the dated copy of ``path`` goes."""
    return path.with_name(f"{path.stem}{DATED_SUFFIX}{path.suffix}")


def modification_time(path: Path) -> datetime:
    """Return ``path``'s modification time in the local timezone."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).astimezone()


def write_date(media: MediaFile, when: datetime, *, dry_run: bool = False) -> DateFix:
    """Write a copy of ``media`` whose date taken is ``when``.

    Everything is checked before anything is written, so a file that cannot
    be dated leaves no copy behind.

    Args:
        media: The JPEG, MP4, MOV, M4V or 3GP file to date.
        when: The date taken; it must carry a timezone.
        dry_run: Check and report the change without writing the copy.

    Raises:
        ValueError: ``when`` has no timezone.
        DateError: The format cannot be dated, the date does not fit, the
            copy's name is taken, or the copy could not be written.
    """
    if when.tzinfo is None:
        raise ValueError("The date taken must carry a timezone")

    try:
        writer = (
            _plan_jpeg(media.path, when)
            if media.kind is MediaKind.IMAGE
            else _plan_movie(media.path, when)
        )
    except OSError as exc:
        raise DateError(str(exc), retryable=False) from exc

    output = dated_path(media.path)
    if output.exists():
        raise DateError(f"{output.name} already exists", retryable=False)

    if not dry_run:
        partial = output.with_name(f".{output.name}.partial")
        try:
            writer(partial)
            shutil.copystat(media.path, partial)
            partial.replace(output)
        except OSError as exc:
            raise DateError(str(exc), retryable=False) from exc
        finally:
            partial.unlink(missing_ok=True)

    return DateFix(
        source=media.path,
        output=output,
        before=read_capture_date(media),
        after=when,
    )


# --- JPEG --------------------------------------------------------------------


def _plan_jpeg(path: Path, when: datetime) -> Writer:
    """Return a writer for ``path`` with ``when`` stamped into its EXIF."""
    try:
        with Image.open(path) as image:
            fmt = image.format
            exif = image.getexif()
    except UnidentifiedImageError as exc:
        raise DateError("is not a readable image", retryable=False) from exc
    if fmt not in _JPEG_FORMATS:
        raise DateError(
            f"only JPEG images can be dated without re-encoding, not {fmt}",
            retryable=False,
        )

    stamp_exif(exif, when)
    payload = exif.tobytes()
    if len(payload) + 2 > _MAX_SEGMENT:
        raise DateError("its EXIF block is too large to rewrite", retryable=False)
    segment = b"\xff" + bytes([_APP1]) + (len(payload) + 2).to_bytes(2, "big")
    dated = _splice_exif(path.read_bytes(), segment + payload)

    def write(target: Path) -> None:
        target.write_bytes(dated)

    return write


def stamp_exif(exif: Image.Exif, when: datetime) -> None:
    """Set the date-taken tags of ``exif`` to ``when``.

    EXIF stores wall-clock time with no zone, so the zone goes alongside in
    the offset tags. A naive ``when`` -- an EXIF date read back from a file --
    already is wall-clock time and gets no offset, rather than an invented one.
    """
    stamp = when.strftime(EXIF_DATETIME_FORMAT)
    sub_ifd = exif.get_ifd(EXIF_IFD_POINTER)
    sub_ifd[DATETIME_ORIGINAL] = stamp
    sub_ifd[DATETIME_DIGITIZED] = stamp
    if when.tzinfo is not None:
        offset = when.strftime("%z")
        sub_ifd[OFFSET_TIME_ORIGINAL] = sub_ifd[OFFSET_TIME_DIGITIZED] = (
            f"{offset[:3]}:{offset[3:]}"
        )


def _splice_exif(data: bytes, segment: bytes) -> bytes:
    """Return ``data`` with its EXIF segment replaced by ``segment``.

    Only the header segments before the image data are walked. Any existing
    EXIF segment is dropped, and the new one goes straight after the start
    marker -- or after a leading JFIF segment, which the JFIF spec wants
    first.
    """
    if not data.startswith(_SOI):
        raise DateError("is not a JPEG file", retryable=False)

    parts = [_SOI]
    inserted = False
    position = len(_SOI)
    while (
        position + 4 <= len(data)
        and data[position] == 0xFF
        and data[position + 1] != _SOS
    ):
        marker = data[position + 1]
        end = position + 2 + int.from_bytes(data[position + 2 : position + 4], "big")
        if end > len(data):
            raise DateError("has a truncated JPEG header", retryable=False)
        if not inserted and marker != _APP0:
            parts.append(segment)
            inserted = True
        is_exif = marker == _APP1 and data[position + 4 : position + 10] == _EXIF_HEADER
        if not is_exif:
            parts.append(data[position:end])
        position = end

    if not inserted:
        parts.append(segment)
    parts.append(data[position:])
    return b"".join(parts)


# --- MP4 / MOV ---------------------------------------------------------------


def _plan_movie(path: Path, when: datetime) -> Writer:
    """Return a writer for ``path`` with ``when`` in its movie and track headers."""
    seconds = int((when - QUICKTIME_EPOCH).total_seconds())
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        moov = find_box(handle, 0, size, b"moov")
        if moov is None:
            raise DateError(
                "only MP4, MOV, M4V and 3GP videos can carry a date iCloud reads; "
                "this one has to be converted first",
                retryable=False,
            )
        if apple_creation_date(handle, *moov) is not None:
            raise DateError(
                "already carries Apple's own capture date, which this cannot change",
                retryable=False,
            )
        stamps = [
            _stamp_for(handle, body_start, seconds)
            for body_start in _time_headers(handle, *moov)
        ]

    def write(target: Path) -> None:
        shutil.copyfile(path, target)
        with target.open("r+b") as out:
            for position, value in stamps:
                out.seek(position)
                out.write(value)

    return write


def _time_headers(handle: BinaryIO, start: int, end: int) -> Iterator[int]:
    """Yield the body start of every header that records creation time."""
    for box_type, body_start, box_end in iter_boxes(handle, start, end):
        if box_type == b"mvhd":
            yield body_start
        elif box_type == b"trak":
            tkhd = find_box(handle, body_start, box_end, b"tkhd")
            if tkhd is not None:
                yield tkhd[0]
            mdia = find_box(handle, body_start, box_end, b"mdia")
            mdhd = None if mdia is None else find_box(handle, *mdia, b"mdhd")
            if mdhd is not None:
                yield mdhd[0]


def _stamp_for(handle: BinaryIO, body_start: int, seconds: int) -> tuple[int, bytes]:
    """Return where to write ``seconds`` into one header, and the bytes to write.

    Creation and modification time sit side by side straight after the
    FullBox version and flags: 32 bits each in version 0, 64 in version 1.
    """
    handle.seek(body_start)
    width = 8 if handle.read(1) == b"\x01" else 4
    if not 0 < seconds < 1 << (8 * width):
        latest = " to 2040" if width == 4 else ""
        raise DateError(
            f"this video can only hold dates from 1904{latest}", retryable=False
        )
    value = seconds.to_bytes(width, "big")
    return body_start + 4, value + value
