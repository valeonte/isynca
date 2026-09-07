"""Walking the local side of a sync.

Unlike :mod:`isynca.media.scanner`, which is looking for uploadable media,
this walk takes everything -- a sync mirrors a folder, and a folder is not
made only of photos. What it *does* filter is noise that would otherwise be
pushed into iCloud or pulled out of it: the daemon's own scratch folders,
desktop metadata, and isynca's partial downloads.

Symlinks are never followed and never synced. A symlink has no content of its
own to upload, and iCloud has nothing to store one as; treating it as a file
would upload whatever it points at under the wrong name, and treating it as a
folder would let a loop walk forever.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from isynca.files.client import PART_SUFFIX
from isynca.logging import get_logger

LOGGER = get_logger("local")

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".com-apple-*",
    ".DS_Store",
    ".Trash*",
    ".fseventsd",
    ".Spotlight-V100",
    f"*{PART_SUFFIX}",
)
"""Names never worth syncing, in either direction.

``.com-apple-*`` earns its place from a real account: ``bird``, the iCloud
Drive daemon, leaves behind empty ``.com-apple-bird-noname-<UUID>`` scratch
folders -- ten of them on the account this was built against. They are
ordinary folders as far as the API is concerned, so only a name rule catches
them, and without one a first sync would reproduce all of that junk locally.
"""


class ExcludeRules:
    """The names a sync ignores, applied identically to both sides.

    Sharing one matcher is the point. An exclusion honoured only locally
    would still let the remote copy be pulled down, which is exactly what
    ``.com-apple-*`` would do: skipped on the way up, then faithfully
    recreated on the way back.
    """

    def __init__(self, extra: Iterable[str] = ()) -> None:
        self.patterns = (*DEFAULT_EXCLUDES, *extra)

    def matches(self, relative: PurePosixPath) -> bool:
        """Return whether a relative path is excluded, itself or by a parent.

        Ancestors are checked explicitly rather than trusted to a glob: a
        pattern written for a folder is meant for everything inside it, and
        that should not depend on whether ``*`` happens to cross a separator.
        """
        return any(self._hits(candidate) for candidate in (relative, *relative.parents))

    def _hits(self, relative: PurePosixPath) -> bool:
        """Return whether one path matches any pattern, by path or by name."""
        if str(relative) == ".":
            return False
        text = str(relative)
        return any(
            fnmatch(text, pattern) or fnmatch(relative.name, pattern)
            for pattern in self.patterns
        )


@dataclass(frozen=True, slots=True)
class LocalFile:
    """One local file, with the stat data change detection keys on."""

    path: PurePosixPath
    """Path relative to the sync root, in POSIX form so it compares to iCloud."""

    absolute: Path
    size: int
    mtime_ns: int


@dataclass(slots=True)
class LocalTree:
    """Everything found under a sync root."""

    files: dict[PurePosixPath, LocalFile] = field(default_factory=dict)
    dirs: dict[PurePosixPath, Path] = field(default_factory=dict)
    excluded: int = 0
    symlinks: int = 0
    unreadable: list[Path] = field(default_factory=list)


class LocalScanner:
    """Walks a sync root and reports every file and folder under it."""

    def __init__(self, exclude: Iterable[str] | ExcludeRules = ()) -> None:
        self.rules = (
            exclude if isinstance(exclude, ExcludeRules) else ExcludeRules(exclude)
        )

    @property
    def exclude(self) -> tuple[str, ...]:
        """Return the patterns in force, defaults included."""
        return self.rules.patterns

    def is_excluded(self, relative: PurePosixPath) -> bool:
        """Return whether a relative path is excluded."""
        return self.rules.matches(relative)

    def scan(self, root: Path) -> LocalTree:
        """Walk ``root`` and return everything under it."""
        tree = LocalTree()
        base = root.expanduser()
        for current, dirnames, filenames in os.walk(
            base, followlinks=False, onerror=lambda exc: self._on_error(tree, exc)
        ):
            here = Path(current)
            relative_dir = _relative(here, base)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if self._keep_dir(tree, here / name, relative_dir / name)
            )
            for name in sorted(filenames):
                self._consider(tree, here / name, relative_dir / name)
        return tree

    def _keep_dir(
        self, tree: LocalTree, absolute: Path, relative: PurePosixPath
    ) -> bool:
        """Record a directory and report whether the walk should enter it."""
        if absolute.is_symlink():
            tree.symlinks += 1
            return False
        if self.is_excluded(relative):
            tree.excluded += 1
            return False
        tree.dirs[relative] = absolute
        return True

    def _consider(
        self, tree: LocalTree, absolute: Path, relative: PurePosixPath
    ) -> None:
        """Record one file, unless it is excluded, a symlink, or unreadable."""
        if absolute.is_symlink():
            tree.symlinks += 1
            return
        if self.is_excluded(relative):
            tree.excluded += 1
            return
        try:
            stat = absolute.stat()
        except OSError as exc:
            LOGGER.warning("Cannot stat %s: %s", absolute, exc)
            tree.unreadable.append(absolute)
            return
        tree.files[relative] = LocalFile(
            path=relative,
            absolute=absolute,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )

    @staticmethod
    def _on_error(tree: LocalTree, exc: OSError) -> None:
        """Record a directory that could not be listed and keep walking."""
        LOGGER.warning("Cannot read directory %s: %s", exc.filename, exc)
        tree.unreadable.append(Path(str(exc.filename)))


def _relative(path: Path, base: Path) -> PurePosixPath:
    """Return ``path`` relative to ``base``, as a POSIX path."""
    return PurePosixPath(path.relative_to(base).as_posix())
