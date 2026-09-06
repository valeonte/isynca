"""Value objects describing what lives in iCloud Drive.

A :class:`RemoteNode` carries every identifier the four write calls need --
``drivewsid`` to address a node, ``docwsid`` to address it as a *container*
or a document, ``etag`` to prove which version is being changed, and ``zone``
to say which store it lives in. Holding all four means the client never has
to go back and re-read a node before acting on it, which matters because
re-reading is what would let a stale listing delete the wrong thing.

Paths are :class:`~pathlib.PurePosixPath` because they are Apple's paths, not
this machine's. Nothing here touches the local filesystem, so a Windows
separator would only ever be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

FILE_TYPE = "file"
"""The ``type`` a document node reports, lowercased."""

FOLDER_TYPE = "folder"
"""The ``type`` an ordinary folder node reports, lowercased."""


class NodeKind(StrEnum):
    """What a remote node behaves as, once its raw type is classified."""

    FILE = "file"
    FOLDER = "folder"


@dataclass(frozen=True, slots=True)
class RemoteNode:
    """One file or folder in iCloud Drive.

    ``raw_type`` is kept alongside :attr:`kind` because Apple returns more
    types than the two isynca acts on -- ``APP_LIBRARY`` for the per-app
    folders at the root of Drive, among others. Classifying to a kind is what
    lets the walker descend; keeping the raw string is what lets a listing
    tell you *why* something was skipped.
    """

    path: PurePosixPath
    kind: NodeKind
    raw_type: str
    drivewsid: str
    docwsid: str
    etag: str
    zone: str
    size: int | None = None
    modified: datetime | None = None

    @property
    def name(self) -> str:
        """Return the node's own name, without its parent folders."""
        return self.path.name

    @property
    def is_dir(self) -> bool:
        """Return whether this node holds other nodes."""
        return self.kind is NodeKind.FOLDER

    @property
    def is_app_library(self) -> bool:
        """Return whether this is a per-app folder rather than a user folder.

        Apple files each app's documents under the root of Drive as its own
        node type. They are folders in every practical sense, but they hold
        application state rather than files a person put there, so isynca
        leaves them alone unless asked not to.
        """
        return self.raw_type not in (FILE_TYPE, FOLDER_TYPE)
