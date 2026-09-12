from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from isynca.files.types import NodeKind, RemoteNode


def make(path: str, kind: NodeKind, raw_type: str) -> RemoteNode:
    return RemoteNode(
        path=PurePosixPath(path),
        kind=kind,
        raw_type=raw_type,
        drivewsid="id",
        docwsid="doc",
        etag="1",
        zone="com.apple.CloudDocs",
    )


def test_name_is_the_last_component():
    assert make("a/b/c.txt", NodeKind.FILE, "file").name == "c.txt"


@pytest.mark.parametrize(
    ("kind", "raw_type", "is_dir", "is_app_library"),
    [
        (NodeKind.FILE, "file", False, False),
        (NodeKind.FOLDER, "folder", True, False),
        (NodeKind.FOLDER, "app_library", True, True),
    ],
)
def test_classification(kind, raw_type, is_dir, is_app_library):
    node = make("x", kind, raw_type)
    assert node.is_dir is is_dir
    assert node.is_app_library is is_app_library
