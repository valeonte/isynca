"""Recursive discovery of media files under one or more source folders."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from isynca.errors import ConfigError
from isynca.logging import get_logger
from isynca.media.types import MediaFile, MediaKind, classify, extensions_for

LOGGER = get_logger("scanner")

DEFAULT_KINDS: frozenset[MediaKind] = frozenset({MediaKind.VIDEO})


@dataclass(slots=True)
class ScanStats:
    """Counts describing what a scan walked past.

    These explain a surprisingly small result set, which is otherwise hard to
    debug across a deep tree.
    """

    files_seen: int = 0
    matched: int = 0
    skipped_extension: int = 0
    skipped_excluded: int = 0
    skipped_too_small: int = 0
    unreadable: list[Path] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        """Return the total number of files skipped for any reason."""
        return self.skipped_extension + self.skipped_excluded + self.skipped_too_small


class Scanner:
    """Walks source folders and yields the media files worth uploading."""

    def __init__(
        self,
        *,
        kinds: frozenset[MediaKind] = DEFAULT_KINDS,
        min_size: int = 0,
        exclude: Iterable[str] = (),
        follow_symlinks: bool = False,
    ) -> None:
        if not kinds:
            raise ConfigError("At least one media kind must be selected")
        self.kinds = kinds
        self.min_size = min_size
        self.exclude = tuple(exclude)
        self.follow_symlinks = follow_symlinks
        self._allowed = extensions_for(kinds)
        self.stats = ScanStats()

    def _is_excluded(self, path: Path) -> bool:
        """Return whether ``path`` matches any exclude glob.

        Each pattern is tried against the full path and the base name, so both
        ``*.tmp`` and ``*/Trash/*`` behave the way a user expects.
        """
        text = str(path)
        return any(
            fnmatch(text, pattern) or fnmatch(path.name, pattern)
            for pattern in self.exclude
        )

    def _consider(self, path: Path) -> MediaFile | None:
        """Classify and stat one candidate file."""
        self.stats.files_seen += 1

        if self._is_excluded(path):
            self.stats.skipped_excluded += 1
            return None

        kind = classify(path)
        if kind is None or kind not in self.kinds:
            self.stats.skipped_extension += 1
            return None

        try:
            stat = path.stat()
        except OSError as exc:
            LOGGER.warning("Cannot stat %s: %s", path, exc)
            self.stats.unreadable.append(path)
            return None

        if stat.st_size < self.min_size:
            self.stats.skipped_too_small += 1
            return None

        self.stats.matched += 1
        return MediaFile(
            path=path, kind=kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns
        )

    def scan(self, sources: Iterable[Path]) -> Iterator[MediaFile]:
        """Yield every matching media file under ``sources``.

        A source may be a single file as well as a directory. Results are
        deduplicated by resolved path so overlapping sources (or a symlink
        loop) cannot yield the same file twice.
        """
        seen: set[Path] = set()
        for source in sources:
            yield from self._scan_one(source, seen)

    def _scan_one(self, source: Path, seen: set[Path]) -> Iterator[MediaFile]:
        """Yield matching files under a single source path."""
        if not source.exists():
            raise ConfigError(f"Source path does not exist: {source}")

        if source.is_file():
            candidate = self._track(source, seen)
            if candidate is not None:
                yield candidate
            return

        for root, dirnames, filenames in os.walk(
            source, followlinks=self.follow_symlinks, onerror=self._on_walk_error
        ):
            root_path = Path(root)
            dirnames[:] = sorted(
                name for name in dirnames if not self._is_excluded(root_path / name)
            )
            for filename in sorted(filenames):
                candidate = self._track(root_path / filename, seen)
                if candidate is not None:
                    yield candidate

    def _track(self, path: Path, seen: set[Path]) -> MediaFile | None:
        """Consider ``path`` unless an equivalent path was already yielded."""
        try:
            key = path.resolve()
        except OSError:  # pragma: no cover - resolve rarely raises on Linux
            key = path.absolute()
        if key in seen:
            return None
        seen.add(key)
        return self._consider(path)

    def _on_walk_error(self, exc: OSError) -> None:
        """Record a directory that could not be listed and keep walking."""
        LOGGER.warning("Cannot read directory %s: %s", exc.filename, exc)
        self.stats.unreadable.append(Path(str(exc.filename)))
