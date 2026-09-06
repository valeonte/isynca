"""An in-memory iCloud Drive, addressed the way Apple addresses the real one.

The fake implements :class:`~isynca.icloud.protocols.DriveServiceLike`, so
everything above the client runs against a dictionary tree instead of a
network. Node records carry Apple's own field names and its split of
``name``/``extension``, because reassembling those is exactly the behaviour
the client is being tested for.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Any

from pyicloud.services.drive import CLOUD_DOCS_ZONE, CLOUD_DOCS_ZONE_ID_ROOT

FOLDER = "FOLDER"
FILE = "FILE"
APP_LIBRARY = "APP_LIBRARY"


@dataclass
class FakeResponse:
    """The streaming half of a ``requests`` response."""

    content: bytes

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        """Yield the body in chunks, including an empty one.

        The empty chunk is not padding: ``requests`` emits them for real, and
        a downloader that counted them as content would corrupt its tally.
        """
        yield b""
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]


@dataclass
class FakeNode:
    """One node of the fake Drive."""

    docwsid: str
    name: str
    node_type: str = FILE
    extension: str | None = None
    content: bytes = b""
    modified: str | None = "2026-01-02T03:04:05Z"
    etag: str = "1"
    children: list[FakeNode] = field(default_factory=list)

    @property
    def drivewsid(self) -> str:
        """Return the node's addressable id."""
        if self.docwsid == "root":
            return CLOUD_DOCS_ZONE_ID_ROOT
        return f"{self.node_type}::{CLOUD_DOCS_ZONE}::{self.docwsid}"

    @property
    def is_dir(self) -> bool:
        """Return whether the node holds children."""
        return self.node_type != FILE

    def record(self) -> dict[str, Any]:
        """Return the node as Apple would describe it in a folder listing."""
        data: dict[str, Any] = {
            "drivewsid": self.drivewsid,
            "docwsid": self.docwsid,
            "zone": CLOUD_DOCS_ZONE,
            "name": self.name,
            "etag": self.etag,
            "type": self.node_type,
        }
        if self.extension is not None:
            data["extension"] = self.extension
        if not self.is_dir:
            data["size"] = len(self.content)
            data["dateModified"] = self.modified
        return data


@dataclass
class FakeDriveService:
    """A Drive service backed by a tree of :class:`FakeNode`."""

    root_node: FakeNode = field(
        default_factory=lambda: FakeNode("root", "root", node_type=FOLDER)
    )
    uploaded: list[tuple[str, str, bytes, dict[str, Any]]] = field(default_factory=list)
    created: list[tuple[str, str]] = field(default_factory=list)
    trashed: list[tuple[str, str]] = field(default_factory=list)
    errors: dict[str, Exception] = field(default_factory=dict)
    unlistable: set[str] = field(default_factory=set)
    missing_items: set[str] = field(default_factory=set)

    def _raise_if_scripted(self, call: str) -> None:
        """Raise the exception a test scripted for ``call``, if any."""
        error = self.errors.get(call)
        if error is not None:
            raise error

    def find(self, drivewsid: str) -> FakeNode | None:
        """Return the node with ``drivewsid``, searching the whole tree."""
        stack = [self.root_node]
        while stack:
            node = stack.pop()
            if node.drivewsid == drivewsid:
                return node
            stack.extend(node.children)
        return None

    def get_node_data(
        self, drivewsid: str, share_id: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return one folder's record, with its children under ``items``."""
        self._raise_if_scripted("get_node_data")
        if drivewsid in self.unlistable:
            raise ValueError(f"cannot list {drivewsid}")
        node = self.find(drivewsid)
        if node is None:
            raise KeyError(drivewsid)
        data = node.record()
        if drivewsid in self.missing_items:
            data["status"] = "NOT_FOUND"
            return data
        data["items"] = [child.record() for child in node.children]
        return data

    def get_file(self, file_id: str, zone: str = CLOUD_DOCS_ZONE, **kwargs: Any) -> Any:
        """Return a streaming response for one document."""
        self._raise_if_scripted("get_file")
        for node in self._walk(self.root_node):
            if node.docwsid == file_id:
                return FakeResponse(node.content)
        raise KeyError(file_id)

    def _walk(self, node: FakeNode) -> Iterator[FakeNode]:
        """Yield ``node`` and everything under it."""
        yield node
        for child in node.children:
            yield from self._walk(child)

    def send_file(
        self,
        folder_id: str,
        file_object: IO[bytes],
        zone: str = CLOUD_DOCS_ZONE,
        **kwargs: Any,
    ) -> None:
        """Record an upload, keeping the name the caller's handle reported.

        The name is what these tests are really about: pyicloud reads it off
        the file object, so a wrapper that reports the wrong one would put a
        path-shaped filename in iCloud.
        """
        self._raise_if_scripted("send_file")
        self.uploaded.append((folder_id, file_object.name, file_object.read(), kwargs))

    def create_folders(self, parent: str, name: str) -> Any:
        """Record a folder creation."""
        self._raise_if_scripted("create_folders")
        self.created.append((parent, name))
        node = self.find(parent)
        if node is not None:
            node.children.append(FakeNode(f"doc-{name}", name, node_type=FOLDER))
        return {"status": "OK"}

    def move_items_to_trash(self, node_id: str, etag: str) -> Any:
        """Record a trashing."""
        self._raise_if_scripted("move_items_to_trash")
        self.trashed.append((node_id, etag))
        return {"status": "OK"}


def build_drive(*children: FakeNode) -> FakeDriveService:
    """Return a Drive whose root holds ``children``."""
    return FakeDriveService(
        root_node=FakeNode("root", "root", node_type=FOLDER, children=list(children))
    )


def folder(name: str, *children: FakeNode) -> FakeNode:
    """Return a folder node called ``name``."""
    return FakeNode(f"doc-{name}", name, node_type=FOLDER, children=list(children))


def document(name: str, content: bytes = b"data", **kwargs: Any) -> FakeNode:
    """Return a file node, splitting its extension the way Apple does."""
    stem, _, extension = name.rpartition(".")
    return FakeNode(
        f"doc-{name}",
        stem or name,
        node_type=FILE,
        extension=extension or None,
        content=content,
        **kwargs,
    )
