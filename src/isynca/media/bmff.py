"""Walking the box tree of an ISO base media file -- MP4, MOV, M4V, 3GP.

Every one of these containers is a sequence of length-prefixed boxes, some of
which nest further boxes. Reading the capture date and rewriting a track's
rotation both come down to finding the right box, so the walker lives here
rather than in either of them.

The walker is defensive by design: a truncated or hostile file ends the walk
early instead of raising or seeking past the data.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO


def iter_boxes(
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


def find_box(
    handle: BinaryIO, start: int, end: int, wanted: bytes
) -> tuple[int, int] | None:
    """Return the body range of the first ``wanted`` box in a range."""
    for box_type, body_start, box_end in iter_boxes(handle, start, end):
        if box_type == wanted:
            return body_start, box_end
    return None
