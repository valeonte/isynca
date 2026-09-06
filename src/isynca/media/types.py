"""Media kinds and the extension tables that classify files.

Scope note: iCloud Photos ingests images and video only -- it has no notion of
a standalone audio asset -- so audio extensions are deliberately absent here.
The tables are keyed by :class:`MediaKind` so a future command can enable
images without touching the scanner.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MediaKind(StrEnum):
    """A class of media file that a backend may accept."""

    VIDEO = "video"
    IMAGE = "image"


VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".3gp",
        ".avi",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".webm",
        ".wmv",
    }
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".avif",
        ".bmp",
        ".dng",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }
)

EXTENSIONS_BY_KIND: dict[MediaKind, frozenset[str]] = {
    MediaKind.VIDEO: VIDEO_EXTENSIONS,
    MediaKind.IMAGE: IMAGE_EXTENSIONS,
}


def classify(path: Path) -> MediaKind | None:
    """Return the media kind of ``path``, or ``None`` if it is not media."""
    suffix = path.suffix.lower()
    for kind, extensions in EXTENSIONS_BY_KIND.items():
        if suffix in extensions:
            return kind
    return None


def extensions_for(kinds: frozenset[MediaKind]) -> frozenset[str]:
    """Return every extension belonging to any of ``kinds``."""
    return frozenset().union(*(EXTENSIONS_BY_KIND[kind] for kind in kinds))


@dataclass(frozen=True, slots=True)
class MediaFile:
    """One media file found on disk, with the stat data the ledger keys on."""

    path: Path
    kind: MediaKind
    size: int
    mtime_ns: int
    source_root: Path
    """The scanned root this file was found under.

    Only the scanner knows which source a file came from, and it cannot be
    recovered afterwards when several overlapping sources were given. It is
    what lets an archive run reproduce the folder structure under a new root.
    """

    @property
    def name(self) -> str:
        """Return the file's base name."""
        return self.path.name

    @property
    def relative_path(self) -> Path:
        """Return this file's path relative to the root it was found under."""
        return self.path.relative_to(self.source_root)
