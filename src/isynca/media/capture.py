"""Reading the capture date -- "date taken" -- out of a media file.

Photos and video keep this in completely different places, so there are two
readers behind one entry point.

Images use EXIF. The true capture timestamp is ``DateTimeOriginal``, but
editors and export pipelines routinely drop it while keeping one of the other
two, so ``DateTimeDigitized`` and then ``DateTime`` are accepted as fallbacks
rather than reporting an obviously-dated photo as undated.

Video has no EXIF at all. MP4 and MOV are ISO base media files, and the date
lives in container atoms: Apple records a real capture timestamp (with a UTC
offset) under ``com.apple.quicktime.creationdate``, and every file carries a
coarser ``mvhd`` creation time. The Apple value is preferred where present
because ``mvhd`` is written by the muxer, so a re-encode overwrites it.

A returned datetime says only that the file carries a date. EXIF timestamps
have no timezone, so those come back naive; container dates come back in UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from isynca.logging import get_logger
from isynca.media.types import MediaFile, MediaKind

LOGGER = get_logger("capture")

# Teaches Pillow to open HEIC/HEIF. Without it every iPhone photo would look
# unreadable, and so would be reported as having no date at all.
register_heif_opener()

EXIF_IFD_POINTER = 0x8769
DATETIME_ORIGINAL = 0x9003
DATETIME_DIGITIZED = 0x9004
DATETIME = 0x0132
EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"

QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
APPLE_CREATIONDATE_KEY = b"com.apple.quicktime.creationdate"

_MAX_METADATA_BOX = 1 << 20
"""Refuse to read an implausibly large keys/ilst atom into memory."""


def read_capture_date(media: MediaFile) -> datetime | None:
    """Return ``media``'s capture date, or ``None`` if it carries none."""
    if media.kind is MediaKind.IMAGE:
        return read_exif_date(media.path)
    return read_container_date(media.path)


def _parse_exif_datetime(value: object) -> datetime | None:
    """Parse an EXIF ``YYYY:MM:DD HH:MM:SS`` string."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\x00", "")
    if not text or text.startswith("0000"):
        return None
    try:
        # EXIF carries no timezone, so this is deliberately naive: stamping it
        # with UTC would invent an offset the file never recorded.
        return datetime.strptime(text, EXIF_DATETIME_FORMAT)  # noqa: DTZ007
    except ValueError:
        return None


def read_exif_date(path: Path) -> datetime | None:
    """Return the EXIF capture date of an image, or ``None``."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            # DateTimeOriginal and DateTimeDigitized live in the Exif sub-IFD,
            # not the top-level one that getexif() returns.
            sub_ifd = exif.get_ifd(EXIF_IFD_POINTER)
            candidates = (
                sub_ifd.get(DATETIME_ORIGINAL),
                sub_ifd.get(DATETIME_DIGITIZED),
                exif.get(DATETIME),
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        LOGGER.debug("Cannot read EXIF from %s: %s", path, exc)
        return None

    for candidate in candidates:
        parsed = _parse_exif_datetime(candidate)
        if parsed is not None:
            return parsed
    return None


def _iter_boxes(
    handle: BinaryIO, start: int, end: int
) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(type, body_start, box_end)`` for each ISO-BMFF box in a range.

    Positions are absolute and the file is re-seeked each iteration, so a
    caller is free to read inside a box before asking for the next one.
    """
    position = start
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(8)
        if len(header) < 8:
            return

        size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_length = 8

        if size == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                return
            size = int.from_bytes(extended, "big")
            header_length = 16
        elif size == 0:
            size = end - position

        if size < header_length or position + size > end:
            return

        yield box_type, position + header_length, position + size
        position += size


def _find_box(
    handle: BinaryIO, start: int, end: int, wanted: bytes
) -> tuple[int, int] | None:
    """Return the body range of the first ``wanted`` box in a range."""
    for box_type, body_start, box_end in _iter_boxes(handle, start, end):
        if box_type == wanted:
            return body_start, box_end
    return None


def _read_keys(handle: BinaryIO, start: int, end: int) -> list[bytes]:
    """Return the metadata key names declared by a ``keys`` box, in order."""
    handle.seek(start + 4)  # skip the FullBox version and flags
    count_bytes = handle.read(4)
    if len(count_bytes) < 4:
        return []

    keys: list[bytes] = []
    position = start + 8
    for _ in range(int.from_bytes(count_bytes, "big")):
        if position + 8 > end:
            break
        handle.seek(position)
        entry_size = int.from_bytes(handle.read(4), "big")
        handle.read(4)  # namespace, e.g. b"mdta"
        if entry_size < 8 or position + entry_size > end:
            break
        keys.append(handle.read(entry_size - 8))
        position += entry_size
    return keys


def _read_creationdate(
    handle: BinaryIO, start: int, end: int, index: int
) -> str | None:
    """Return the string value stored in an ``ilst`` entry for ``index``."""
    for box_type, body_start, box_end in _iter_boxes(handle, start, end):
        if int.from_bytes(box_type, "big") != index:
            continue
        data = _find_box(handle, body_start, box_end, b"data")
        if data is None:
            return None
        data_start, data_end = data
        if data_end - data_start > _MAX_METADATA_BOX:
            return None
        handle.seek(data_start + 8)  # skip the type and locale words
        return handle.read(data_end - data_start - 8).decode("utf-8", "replace")
    return None


def _apple_creation_date(handle: BinaryIO, start: int, end: int) -> datetime | None:
    """Return Apple's QuickTime creation date from a ``moov`` box."""
    meta = _find_box(handle, start, end, b"meta")
    if meta is None:
        return None
    meta_start, meta_end = meta

    # QuickTime writes `meta` as a plain box while ISO writes it as a FullBox
    # with a 4-byte version/flags prefix. Try both rather than guess.
    for offset in (0, 4):
        keys_range = _find_box(handle, meta_start + offset, meta_end, b"keys")
        ilst_range = _find_box(handle, meta_start + offset, meta_end, b"ilst")
        if keys_range is None or ilst_range is None:
            continue
        if keys_range[1] - keys_range[0] > _MAX_METADATA_BOX:
            continue

        keys = _read_keys(handle, *keys_range)
        if APPLE_CREATIONDATE_KEY not in keys:
            continue
        # ilst entries are 1-indexed against the keys table.
        index = keys.index(APPLE_CREATIONDATE_KEY) + 1
        raw = _read_creationdate(handle, *ilst_range, index)
        if raw is None:
            continue
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            LOGGER.debug("Unparseable QuickTime creation date %r", raw)
    return None


def _mvhd_creation_date(handle: BinaryIO, start: int, end: int) -> datetime | None:
    """Return the ``mvhd`` creation time from a ``moov`` box."""
    mvhd = _find_box(handle, start, end, b"mvhd")
    if mvhd is None:
        return None

    handle.seek(mvhd[0])
    version_flags = handle.read(4)
    if len(version_flags) < 4:
        return None
    width = 8 if version_flags[0] == 1 else 4

    raw = handle.read(width)
    if len(raw) < width:
        return None
    seconds = int.from_bytes(raw, "big")
    if seconds == 0:
        # Plenty of muxers leave this zeroed, which is an absent date rather
        # than a video shot in 1904.
        return None
    try:
        return QUICKTIME_EPOCH + timedelta(seconds=seconds)
    except OverflowError, ValueError:
        # A corrupt field can hold a value no datetime can represent.
        return None


def read_container_date(path: Path) -> datetime | None:
    """Return the capture date recorded in a video container, or ``None``."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            moov = _find_box(handle, 0, size, b"moov")
            if moov is None:
                return None
            return _apple_creation_date(handle, *moov) or _mvhd_creation_date(
                handle, *moov
            )
    except OSError as exc:
        LOGGER.debug("Cannot read container metadata from %s: %s", path, exc)
        return None
