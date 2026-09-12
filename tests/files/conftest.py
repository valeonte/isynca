from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from isynca.files.local import LocalFile, LocalTree
from isynca.files.state import EntryKind, SyncRecord
from isynca.files.types import NodeKind, RemoteNode

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def make_local(tmp_path: Path):
    """Return a factory writing a real file and describing it as local."""

    def factory(path: str, content: bytes = b"data", mtime_ns: int | None = None):
        absolute = tmp_path / path
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(content)
        stat = absolute.stat()
        return LocalFile(
            path=PurePosixPath(path),
            absolute=absolute,
            size=stat.st_size,
            mtime_ns=mtime_ns if mtime_ns is not None else stat.st_mtime_ns,
        )

    return factory


def remote_file(path: str, size: int = 4, etag: str = "e1", modified=STAMP):
    """Return a remote file node."""
    return RemoteNode(
        path=PurePosixPath(path),
        kind=NodeKind.FILE,
        raw_type="file",
        drivewsid=f"FILE::z::{path}",
        docwsid=path,
        etag=etag,
        zone="com.apple.CloudDocs",
        size=size,
        modified=modified,
    )


def remote_dir(path: str, etag: str = "d1"):
    """Return a remote folder node."""
    return RemoteNode(
        path=PurePosixPath(path),
        kind=NodeKind.FOLDER,
        raw_type="folder",
        drivewsid=f"FOLDER::z::{path}",
        docwsid=path,
        etag=etag,
        zone="com.apple.CloudDocs",
    )


def record_for(local: LocalFile, remote: RemoteNode, content_hash: str = "h1"):
    """Return the state row that would follow a successful sync of a pair."""
    return SyncRecord(
        path=local.path,
        kind=EntryKind.FILE,
        size=local.size,
        mtime_ns=local.mtime_ns,
        content_hash=content_hash,
        etag=remote.etag,
        remote_size=remote.size or 0,
        remote_modified=remote.modified.isoformat() if remote.modified else None,
    )


def dir_record(path: str, etag: str = "d1"):
    """Return the state row for a folder both sides hold."""
    return SyncRecord(path=PurePosixPath(path), kind=EntryKind.FOLDER, etag=etag)


def tree(files=(), dirs=()) -> LocalTree:
    """Return a LocalTree from the given files and directory paths."""
    return LocalTree(
        files={item.path: item for item in files},
        dirs={PurePosixPath(d): Path(d) for d in dirs},
    )


def index(*nodes: RemoteNode):
    """Index remote nodes by path."""
    return {node.path: node for node in nodes}
