"""Natural ordering: ``photo (2).jpg`` before ``photo (10).jpg``.

Plain string order puts ``(1 of 231)``, ``(10 of 231)``, ``(100 of 231)`` and
the rest of the hundreds before ``(2 of 231)``, which makes a run over a
numbered export look as though it is skipping about. Every place isynca
decides what order to walk or act on files uses these keys instead, so that
digit runs compare by value and everything else by text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import PurePath

_DIGITS = re.compile(r"(\d+)")

type Chunk = tuple[int, int, str] | tuple[int, str, str]
type NaturalKey = tuple[Chunk, ...]


def natural_key(text: str) -> NaturalKey:
    """Return a sort key that compares digit runs by value.

    Each chunk is tagged so that numbers always sort before text at the same
    position and never get compared against it. A number keeps its original
    spelling as a tie-break, so ``01`` and ``1`` stay distinct and stable.
    """
    return tuple(
        (0, int(chunk), chunk) if chunk.isdigit() else (1, chunk, "")
        for chunk in _DIGITS.split(text)
        if chunk
    )


def natural_path_key(path: PurePath) -> tuple[NaturalKey, ...]:
    """Return a sort key ordering paths component by component, naturally.

    Comparing by component rather than by the joined string keeps a folder's
    contents together: ``a/b`` sorts next to ``a/c`` rather than wherever the
    separator happens to fall against the neighbouring names.
    """
    return tuple(natural_key(part) for part in path.parts)
