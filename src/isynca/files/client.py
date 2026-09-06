"""Adapter over pyicloud's Drive service.

Everything above this module works in :class:`~isynca.files.types.RemoteNode`
values and local paths; this is the only place that knows Apple's identifiers,
its date format, and its two-step upload.

Three details of the wire protocol shape the code here:

*Uploads are named by the file object.* ``send_file`` reads the remote
filename off ``file_object.name`` -- there is no separate argument for it --
so uploading a handle opened from an absolute path would ask iCloud to store
a file called ``/home/you/notes.md``. :class:`NamedReader` is what stops that.

*Zero-byte files cannot be downloaded.* Apple answers the download endpoint
with 400 for an empty document, so an empty file is created locally rather
than fetched.

*A folder listing is a network call per folder.* There is no recursive
listing endpoint, so walking a large Drive costs one round trip per folder.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast

from pyicloud.exceptions import PyiCloudException
from pyicloud.services.drive import CLOUD_DOCS_ZONE, CLOUD_DOCS_ZONE_ID_ROOT
from requests import RequestException

from isynca.errors import (
    DriveError,
    DriveListingError,
    DriveNotAvailableError,
)
from isynca.files.types import FILE_TYPE, NodeKind, RemoteNode
from isynca.icloud.protocols import DriveServiceLike, ICloudSessionLike
from isynca.logging import get_logger

LOGGER = get_logger("drive")

PART_SUFFIX = ".isynca-part"
"""Extension a download carries until it is complete.

Downloads land on a neighbouring temporary file and are renamed into place
only once the last byte has arrived, so an interrupted run leaves a visibly
partial file rather than a plausible-looking truncated one.
"""

_TRANSPORT_ERRORS = (PyiCloudException, RequestException, KeyError, ValueError)
"""What a Drive call can fail with.

``KeyError`` is in there because pyicloud raises it rather than an API error
when a download response carries neither a data nor a package token, and
``ValueError`` because a malformed JSON body surfaces that way.
"""


class NamedReader:
    """A binary stream that reports a name of our choosing.

    ``DriveService.send_file`` takes the remote filename from
    ``file_object.name`` in three separate places -- the upload registration,
    the MIME-type guess, and the multipart field name -- and offers no
    argument to override it. A handle from ``open()`` reports the path it was
    opened with, so uploading ``/home/you/notes.md`` would name the remote
    document after the whole path.

    Wrapping the handle and reporting the base name fixes all three at once.
    Only the methods ``requests`` and pyicloud actually reach for are
    delegated; the wrapper is deliberately not a general file object.
    """

    def __init__(self, stream: IO[bytes], name: str) -> None:
        self._stream = stream
        self.name = name

    def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes from the wrapped stream."""
        return self._stream.read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Seek the wrapped stream; pyicloud measures the file this way."""
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        """Return the wrapped stream's position."""
        return self._stream.tell()


def parse_date(value: str | None) -> datetime | None:
    """Parse one of Apple's timestamps into an aware UTC datetime.

    A value that will not parse is reported as absent rather than raised
    over: a date isynca cannot read is a reason to fall back on size and
    etag, not a reason to abandon the file.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        LOGGER.debug("Ignoring unparseable timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def node_name(item: dict[str, Any]) -> str:
    """Return a node's filename, reattaching the extension Apple splits off.

    Apple stores ``notes.md`` as a name of ``notes`` and an extension of
    ``md``. Rejoining them here is what makes a remote path comparable with a
    local one.
    """
    name = str(item.get("name") or item.get("drivewsid") or "")
    extension = item.get("extension")
    return f"{name}.{extension}" if extension else name


class DriveClient:
    """Reads and writes iCloud Drive in terms of paths and value objects."""

    def __init__(self, session: ICloudSessionLike) -> None:
        try:
            self._drive: DriveServiceLike = session.drive
        except (PyiCloudException, AttributeError) as exc:
            raise DriveNotAvailableError(
                f"This account exposes no iCloud Drive service: {exc}"
            ) from exc

    def root(self) -> RemoteNode:
        """Return the root of iCloud Drive, as Finder shows it.

        This is the ``com.apple.CloudDocs`` zone specifically -- the folder
        called "iCloud Drive" -- not the account's whole document store.
        """
        data = self._node_data(CLOUD_DOCS_ZONE_ID_ROOT)
        return RemoteNode(
            path=PurePosixPath("."),
            kind=NodeKind.FOLDER,
            raw_type=str(data.get("type", "FOLDER")).lower(),
            drivewsid=str(data.get("drivewsid", CLOUD_DOCS_ZONE_ID_ROOT)),
            docwsid=str(data.get("docwsid", "root")),
            etag=str(data.get("etag", "")),
            zone=str(data.get("zone", CLOUD_DOCS_ZONE)),
        )

    def walk(
        self, *, include_app_libraries: bool = False, depth: int = 0
    ) -> Iterator[RemoteNode]:
        """Yield every node under the Drive root, parents before children.

        Folders come out before their contents so a caller can create a
        directory before writing into it without sorting anything itself.

        ``depth`` bounds how far to descend, ``0`` meaning no limit. It stops
        the walk rather than filtering its output, which matters because each
        level costs one network round trip per folder: listing the top of a
        Drive should not pay for the whole of it.
        """
        yield from self._walk(
            CLOUD_DOCS_ZONE_ID_ROOT, PurePosixPath(), include_app_libraries, depth
        )

    def _walk(
        self,
        drivewsid: str,
        prefix: PurePosixPath,
        include_app_libraries: bool,
        remaining: int,
    ) -> Iterator[RemoteNode]:
        """Yield the subtree rooted at ``drivewsid``, depth first."""
        for item in self._items(drivewsid):
            node = self._to_node(item, prefix)
            if node.is_app_library and not include_app_libraries:
                LOGGER.debug(
                    "Skipping %s: %s is an app library", node.path, node.raw_type
                )
                continue
            yield node
            if node.is_dir and remaining != 1:
                yield from self._walk(
                    node.drivewsid,
                    node.path,
                    include_app_libraries,
                    max(remaining - 1, 0),
                )

    def children(self, folder: RemoteNode) -> list[RemoteNode]:
        """Return the immediate children of one folder."""
        return [
            self._to_node(item, folder.path) for item in self._items(folder.drivewsid)
        ]

    def _items(self, drivewsid: str) -> list[dict[str, Any]]:
        """Return the raw child records of one folder.

        A folder that will not list stops the run. It is indistinguishable
        from an empty one, and an empty one is what tells a sync that
        everything inside was deleted.
        """
        data = self._node_data(drivewsid)
        items = data.get("items")
        if items is None:
            raise DriveListingError(
                f"iCloud listed no contents for {drivewsid} "
                f"(status {data.get('status', 'unknown')})"
            )
        return list(items)

    def _node_data(self, drivewsid: str) -> dict[str, Any]:
        """Fetch one node's record, translating transport failures."""
        try:
            return self._drive.get_node_data(drivewsid)
        except _TRANSPORT_ERRORS as exc:
            raise DriveListingError(f"Could not read {drivewsid}: {exc}") from exc

    def _to_node(self, item: dict[str, Any], prefix: PurePosixPath) -> RemoteNode:
        """Build a :class:`RemoteNode` from one raw child record."""
        raw_type = str(item.get("type", "")).lower()
        size = item.get("size")
        return RemoteNode(
            path=prefix / node_name(item),
            kind=NodeKind.FILE if raw_type == FILE_TYPE else NodeKind.FOLDER,
            raw_type=raw_type,
            drivewsid=str(item.get("drivewsid", "")),
            docwsid=str(item.get("docwsid", "")),
            etag=str(item.get("etag", "")),
            zone=str(item.get("zone", CLOUD_DOCS_ZONE)),
            size=None if size is None else int(size),
            modified=parse_date(item.get("dateModified")),
        )

    def download(self, node: RemoteNode, destination: Path) -> int:
        """Fetch one document to ``destination`` and return its byte count.

        The content lands on a temporary neighbour and is renamed into place
        at the end, so a half-finished download is never mistaken for the
        file itself. The remote modification time is stamped onto the result,
        which is what keeps the next run from seeing a fresh local edit.

        Raises:
            DriveError: The download failed.
        """
        partial = destination.with_name(destination.name + PART_SUFFIX)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = self._write_content(node, partial)
            partial.replace(destination)
        except (OSError, *_TRANSPORT_ERRORS) as exc:
            partial.unlink(missing_ok=True)
            raise DriveError(
                f"Could not download {node.path} to {destination}: {exc}"
            ) from exc

        self._stamp(destination, node.modified)
        LOGGER.info("Downloaded %s (%d bytes)", node.path, written)
        return written

    def _write_content(self, node: RemoteNode, partial: Path) -> int:
        """Stream one document's bytes onto disk and return the count.

        Apple answers the download endpoint with 400 for a zero-byte
        document, so an empty file is written rather than requested.
        """
        if node.size == 0:
            partial.write_bytes(b"")
            return 0

        response = self._drive.get_file(node.docwsid, zone=node.zone, stream=True)
        written = 0
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
        return written

    @staticmethod
    def _stamp(path: Path, modified: datetime | None) -> None:
        """Set ``path``'s modification time from the remote's, if there is one."""
        if modified is None:
            return
        try:
            os.utime(path, (modified.timestamp(), modified.timestamp()))
        except OSError as exc:  # pragma: no cover - defensive
            LOGGER.debug("Could not stamp %s: %s", path, exc)

    def upload(self, parent: RemoteNode, source: Path) -> None:
        """Send ``source`` into the ``parent`` folder under its own name.

        Raises:
            DriveError: The upload failed.
        """
        try:
            stat = source.stat()
            with source.open("rb") as handle:
                self._drive.send_file(
                    parent.docwsid,
                    # NamedReader is deliberately not a whole file object --
                    # it implements the three methods this call path uses and
                    # nothing else, which no ``IO[bytes]`` annotation can say.
                    cast(IO[bytes], NamedReader(handle, source.name)),
                    zone=parent.zone,
                    mtime=stat.st_mtime,
                    ctime=stat.st_mtime,
                )
        except (OSError, *_TRANSPORT_ERRORS) as exc:
            raise DriveError(f"Could not upload {source}: {exc}") from exc
        LOGGER.info("Uploaded %s -> %s", source, parent.path / source.name)

    def mkdir(self, parent: RemoteNode, name: str) -> None:
        """Create a folder called ``name`` under ``parent``.

        Raises:
            DriveError: The folder could not be created.
        """
        try:
            self._drive.create_folders(parent.drivewsid, name)
        except _TRANSPORT_ERRORS as exc:
            raise DriveError(
                f"Could not create folder {parent.path / name}: {exc}"
            ) from exc
        LOGGER.info("Created remote folder %s", parent.path / name)

    def trash(self, node: RemoteNode) -> None:
        """Move one node into Recently Deleted.

        Trash rather than delete, always: a sync that got its diff wrong
        should be recoverable for thirty days, not permanent.

        Raises:
            DriveError: The node could not be trashed.
        """
        try:
            self._drive.move_items_to_trash(node.drivewsid, node.etag)
        except _TRANSPORT_ERRORS as exc:
            raise DriveError(f"Could not delete {node.path}: {exc}") from exc
        LOGGER.info("Trashed %s", node.path)
