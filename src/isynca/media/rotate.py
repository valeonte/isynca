"""Rotating a video without re-encoding it.

An MP4 or MOV does not store its frames the way up they should be shown. Each
video track's ``tkhd`` box carries a 3x3 display matrix, and players apply it
when drawing the frames. Turning a sideways video upright is therefore a
rewrite of 36 bytes per video track: nothing is decoded or re-encoded, and
every other byte -- the ``mvhd`` and ``com.apple.quicktime.creationdate``
capture dates included -- is carried over exactly as it was.

The original is never modified. The rotated video is written beside it as
``<stem>_rot<degrees><suffix>``, and only appears under that name once it is
complete, so an interrupted run leaves no half-written copy behind.

QuickTime maps a frame point ``(x, y)`` to ``(a*x + c*y + tx, b*x + d*y + ty)``
with ``y`` pointing down the screen, so a quarter turn clockwise sends "right",
``(1, 0)``, to "down", ``(0, 1)``. The translation moves the turned frame back
into positive coordinates; that is the layout Apple's own cameras write, and
what AVFoundation expects.

Only pure rotations are recognised. A track whose matrix mirrors or scales the
frame is refused rather than silently flattened into a rotation.
"""

from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from isynca.errors import RotationError
from isynca.media.bmff import find_box, iter_boxes

QUARTER_TURNS: tuple[int, ...] = (90, 180, 270)
"""The clockwise rotations a caller may ask for."""

_ONE = 1 << 16
"""1.0 in the matrix's 16.16 fixed-point cells."""

_W_ONE = 1 << 30
"""1.0 in the matrix's bottom-right cell, which is 2.30 fixed point."""

_ROTATION_BY_CELLS: dict[tuple[int, int, int, int], int] = {
    (_ONE, 0, 0, _ONE): 0,
    (0, _ONE, -_ONE, 0): 90,
    (-_ONE, 0, 0, -_ONE): 180,
    (0, -_ONE, _ONE, 0): 270,
}
"""Clockwise degrees keyed by the ``(a, b, c, d)`` cells of the matrix."""

_CELLS_BY_ROTATION = {degrees: cells for cells, degrees in _ROTATION_BY_CELLS.items()}

_MATRIX = struct.Struct(">9i")
_SIZE = struct.Struct(">II")

# Where the matrix sits inside a tkhd body. After the 4-byte version/flags,
# version 0 has 20 bytes of times, track ID and duration (version 1 widens the
# times and duration to 64 bits, making 32), then 16 bytes of reserved words,
# layer, alternate group and volume.
_MATRIX_OFFSET_V0 = 40
_MATRIX_OFFSET_V1 = 52


@dataclass(frozen=True, slots=True)
class Rotation:
    """One rotated copy: where it came from, where it went, and how it turns."""

    source: Path
    output: Path
    before: int
    after: int
    """Clockwise degrees the video is displayed with, before and after."""


@dataclass(frozen=True, slots=True)
class _TrackHeader:
    """The parts of one video track's ``tkhd`` a rotation needs."""

    matrix_offset: int
    """Absolute file position of the display matrix."""

    rotation: int
    """Clockwise degrees the matrix currently applies."""

    width: int
    height: int
    """Track dimensions, as the raw 16.16 values the translation is built from."""


def read_rotation(path: Path) -> int:
    """Return the clockwise rotation a video is displayed with, in degrees.

    Raises:
        RotationError: The file is unreadable, is not an MP4/MOV, holds no
            video track, or is displayed mirrored or scaled.
    """
    return _rotate(path, 0, write=False)


def rotated_path(path: Path, clockwise: int) -> Path:
    """Return where the copy of ``path`` turned ``clockwise`` degrees goes."""
    return path.with_name(f"{path.stem}_rot{clockwise}{path.suffix}")


def rotate_video(path: Path, clockwise: int, *, dry_run: bool = False) -> Rotation:
    """Write a copy of ``path`` turned a further ``clockwise`` degrees.

    The copy goes to :func:`rotated_path` and keeps the original's
    modification time. The original is only ever read, and is checked in full
    before anything is written, so a file that cannot be rotated leaves no
    copy behind.

    Args:
        path: The MP4, MOV, M4V or 3GP file to rotate.
        clockwise: 90, 180 or 270, added to whatever rotation it already has.
        dry_run: Check and report the change without writing the copy.

    Raises:
        ValueError: ``clockwise`` is not a quarter turn.
        RotationError: The file cannot be rotated (see :func:`read_rotation`),
            the copy's name is already taken, or the copy could not be written.
    """
    if clockwise not in QUARTER_TURNS:
        raise ValueError(f"Rotation must be one of {QUARTER_TURNS}, not {clockwise}")

    before = read_rotation(path)
    output = rotated_path(path, clockwise)
    if output.exists():
        raise RotationError(f"{output.name} already exists", retryable=False)

    if not dry_run:
        partial = output.with_name(f".{output.name}.partial")
        try:
            shutil.copyfile(path, partial)
            _rotate(partial, clockwise, write=True)
            # After the write, which would otherwise stamp it with "now".
            shutil.copystat(path, partial)
            partial.replace(output)
        except OSError as exc:
            raise RotationError(str(exc), retryable=False) from exc
        finally:
            partial.unlink(missing_ok=True)

    return Rotation(
        source=path, output=output, before=before, after=(before + clockwise) % 360
    )


def _rotate(path: Path, clockwise: int, *, write: bool) -> int:
    """Return the rotation of ``path``, first turning it in place if asked."""
    try:
        with path.open("r+b" if write else "rb") as handle:
            size = handle.seek(0, 2)
            tracks = _video_tracks(handle, size)
            if write:
                for track in tracks:
                    handle.seek(track.matrix_offset)
                    handle.write(_matrix(track, (track.rotation + clockwise) % 360))
    except OSError as exc:
        raise RotationError(str(exc), retryable=False) from exc
    return tracks[0].rotation


def _video_tracks(handle: BinaryIO, size: int) -> list[_TrackHeader]:
    """Return the header of every video track in the file."""
    moov = find_box(handle, 0, size, b"moov")
    if moov is None:
        raise RotationError(
            "not an MP4/QuickTime file; only MP4, MOV, M4V and 3GP can be "
            "rotated without re-encoding",
            retryable=False,
        )

    tracks = [
        _track_header(handle, body_start, box_end)
        for box_type, body_start, box_end in iter_boxes(handle, *moov)
        if box_type == b"trak" and _is_video(handle, body_start, box_end)
    ]
    if not tracks:
        raise RotationError("holds no video track", retryable=False)
    return tracks


def _is_video(handle: BinaryIO, start: int, end: int) -> bool:
    """Return whether the ``trak`` in a range is a video track."""
    mdia = find_box(handle, start, end, b"mdia")
    if mdia is None:
        return False
    hdlr = find_box(handle, *mdia, b"hdlr")
    if hdlr is None:
        return False
    # FullBox version/flags, then a pre_defined word, then the handler type.
    handle.seek(hdlr[0] + 8)
    return handle.read(4) == b"vide"


def _track_header(handle: BinaryIO, start: int, end: int) -> _TrackHeader:
    """Read the display matrix and size out of a video ``trak``."""
    tkhd = find_box(handle, start, end, b"tkhd")
    if tkhd is None:
        raise RotationError("has a video track with no header", retryable=False)
    body_start, body_end = tkhd

    handle.seek(body_start)
    version = handle.read(1)
    offset = _MATRIX_OFFSET_V1 if version == b"\x01" else _MATRIX_OFFSET_V0
    raw = b""
    if body_start + offset + _MATRIX.size + _SIZE.size <= body_end:
        handle.seek(body_start + offset)
        raw = handle.read(_MATRIX.size + _SIZE.size)
    if len(raw) < _MATRIX.size + _SIZE.size:
        raise RotationError("has a truncated video track header", retryable=False)

    a, b, _u, c, d, *_ = _MATRIX.unpack(raw[: _MATRIX.size])
    rotation = _ROTATION_BY_CELLS.get((a, b, c, d))
    if rotation is None:
        raise RotationError(
            "is displayed mirrored or scaled, not simply rotated", retryable=False
        )
    width, height = _SIZE.unpack(raw[_MATRIX.size :])
    return _TrackHeader(
        matrix_offset=body_start + offset,
        rotation=rotation,
        width=width,
        height=height,
    )


def _matrix(track: _TrackHeader, degrees: int) -> bytes:
    """Build the display matrix that turns ``track`` ``degrees`` clockwise."""
    a, b, c, d = _CELLS_BY_ROTATION[degrees]
    tx, ty = {
        0: (0, 0),
        90: (track.height, 0),
        180: (track.width, track.height),
        270: (0, track.width),
    }[degrees]
    return _MATRIX.pack(a, b, 0, c, d, 0, tx, ty, _W_ONE)
