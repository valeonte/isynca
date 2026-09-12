from __future__ import annotations

import errno
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException
from pyicloud.services.drive import CLOUD_DOCS_ZONE_ID_ROOT

from isynca.errors import DriveError, DriveListingError, DriveNotAvailableError
from isynca.files.client import DriveClient, NamedReader, node_name, parse_date
from isynca.files.types import NodeKind
from tests.fakes.drive import (
    APP_LIBRARY,
    FakeNode,
    build_drive,
    document,
    folder,
)
from tests.fakes.icloud import FakeSession


@pytest.fixture
def drive():
    """A small Drive: one loose file, one nested folder, one app library."""
    return build_drive(
        document("top.txt", b"top"),
        folder("Notes", document("todo.md", b"todo"), folder("Sub")),
        FakeNode("doc-pages", "com.apple.Pages", node_type=APP_LIBRARY),
    )


@pytest.fixture
def client(drive):
    return DriveClient(FakeSession(drive_service=drive))


def test_walk_yields_parents_before_children(client):
    paths = [str(node.path) for node in client.walk()]
    assert paths == ["top.txt", "Notes", "Notes/todo.md", "Notes/Sub"]


def test_walk_skips_app_libraries_by_default(client):
    assert "com.apple.Pages" not in [str(n.path) for n in client.walk()]


def test_walk_includes_app_libraries_when_asked(client):
    paths = [str(n.path) for n in client.walk(include_app_libraries=True)]
    assert "com.apple.Pages" in paths


def test_walk_classifies_kinds_and_metadata(client):
    nodes = {str(node.path): node for node in client.walk()}
    assert nodes["top.txt"].kind is NodeKind.FILE
    assert nodes["top.txt"].size == 3
    assert nodes["top.txt"].modified == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert nodes["Notes"].kind is NodeKind.FOLDER
    assert nodes["Notes"].size is None
    assert nodes["Notes"].is_dir


def test_root_describes_the_clouddocs_root(client):
    root = client.root()
    assert root.drivewsid == CLOUD_DOCS_ZONE_ID_ROOT
    assert root.docwsid == "root"
    assert root.is_dir
    assert str(root.path) == "."


def test_children_returns_one_level(client):
    names = [child.name for child in client.children(client.root())]
    assert names == ["top.txt", "Notes", "com.apple.Pages"]


def test_unlistable_folder_stops_the_run(drive, client):
    drive.unlistable.add("FOLDER::com.apple.CloudDocs::doc-Notes")
    with pytest.raises(DriveListingError, match="Could not read"):
        list(client.walk())


def test_folder_without_items_stops_the_run(drive, client):
    drive.missing_items.add(CLOUD_DOCS_ZONE_ID_ROOT)
    with pytest.raises(DriveListingError, match="NOT_FOUND"):
        list(client.walk())


def test_missing_drive_service_is_fatal():
    session = FakeSession(drive_error=PyiCloudAPIResponseException("gone", 404))
    with pytest.raises(DriveNotAvailableError, match="no iCloud Drive"):
        DriveClient(session)


def test_download_writes_content_and_stamps_mtime(client, tmp_path):
    node = next(n for n in client.walk() if n.name == "top.txt")
    dest = tmp_path / "nested" / "top.txt"
    assert client.download(node, dest) == 3
    assert dest.read_bytes() == b"top"
    assert dest.stat().st_mtime == pytest.approx(node.modified.timestamp())


def test_download_of_empty_file_never_asks_apple(drive, client, tmp_path):
    drive.root_node.children.append(document("empty.txt", b""))
    drive.errors["get_file"] = AssertionError("must not be called")
    node = next(n for n in client.walk() if n.name == "empty.txt")
    assert client.download(node, tmp_path / "empty.txt") == 0
    assert (tmp_path / "empty.txt").read_bytes() == b""


def test_download_without_a_date_leaves_mtime_alone(drive, client, tmp_path):
    drive.root_node.children.append(document("undated.txt", b"x", modified=None))
    node = next(n for n in client.walk() if n.name == "undated.txt")
    client.download(node, tmp_path / "undated.txt")
    assert (tmp_path / "undated.txt").read_bytes() == b"x"


def test_failed_download_leaves_no_partial_file(drive, client, tmp_path):
    drive.errors["get_file"] = PyiCloudAPIResponseException("boom", 500)
    node = next(n for n in client.walk() if n.name == "top.txt")
    with pytest.raises(DriveError, match="Could not download"):
        client.download(node, tmp_path / "top.txt")
    assert list(tmp_path.iterdir()) == []


def test_upload_sends_the_base_name_not_the_path(drive, client, tmp_path):
    source = tmp_path / "deep" / "notes.md"
    source.parent.mkdir()
    source.write_bytes(b"hello")

    client.upload(client.root(), source)

    folder_id, name, content, kwargs = drive.uploaded[0]
    assert name == "notes.md"
    assert folder_id == "root"
    assert content == b"hello"
    assert kwargs["mtime"] == pytest.approx(source.stat().st_mtime)


def test_upload_failure_is_an_item_error(drive, client, tmp_path):
    drive.errors["send_file"] = PyiCloudAPIResponseException("nope", 503)
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    with pytest.raises(DriveError, match="Could not upload"):
        client.upload(client.root(), source)


def test_a_dropped_connection_during_upload_is_flagged_as_transport(
    drive, client, tmp_path
):
    """The runner waits for a network that went, rather than failing the file."""
    drive.errors["send_file"] = OSError(errno.EHOSTUNREACH, "No route to host")
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    with pytest.raises(DriveError) as raised:
        client.upload(client.root(), source)

    assert raised.value.transport


def test_a_rejected_upload_is_not_flagged_as_transport(drive, client, tmp_path):
    """ICloud answered, so waiting for the network would be waiting for nothing."""
    drive.errors["send_file"] = PyiCloudAPIResponseException("nope", 503)
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    with pytest.raises(DriveError) as raised:
        client.upload(client.root(), source)

    assert not raised.value.transport


def test_upload_of_a_missing_file_is_an_item_error(client, tmp_path):
    with pytest.raises(DriveError, match="Could not upload"):
        client.upload(client.root(), tmp_path / "absent.txt")


def test_mkdir_creates_under_the_parent(drive, client):
    client.mkdir(client.root(), "New")
    assert drive.created == [(CLOUD_DOCS_ZONE_ID_ROOT, "New")]


def test_mkdir_failure_is_an_item_error(drive, client):
    drive.errors["create_folders"] = PyiCloudAPIResponseException("nope", 500)
    with pytest.raises(DriveError, match="Could not create folder"):
        client.mkdir(client.root(), "New")


def test_trash_moves_to_recently_deleted(drive, client):
    node = next(n for n in client.walk() if n.name == "top.txt")
    client.trash(node)
    assert drive.trashed == [(node.drivewsid, node.etag)]


def test_trash_failure_is_an_item_error(drive, client):
    drive.errors["move_items_to_trash"] = PyiCloudAPIResponseException("nope", 500)
    node = next(n for n in client.walk() if n.name == "top.txt")
    with pytest.raises(DriveError, match="Could not delete"):
        client.trash(node)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02T03:04:05+02:00", datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC)),
        ("2026-01-02T03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"name": "notes", "extension": "md"}, "notes.md"),
        ({"name": "README"}, "README"),
        ({"drivewsid": "FOLDER::x::y"}, "FOLDER::x::y"),
        ({}, ""),
    ],
)
def test_node_name_reattaches_the_extension(item, expected):
    assert node_name(item) == expected


def test_named_reader_delegates_to_the_stream(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"abcdef")
    with path.open("rb") as handle:
        reader = NamedReader(handle, "a.bin")
        assert reader.name == "a.bin"
        assert reader.read(3) == b"abc"
        assert reader.tell() == 3
        assert reader.seek(0) == 0
        assert reader.read() == b"abcdef"


def test_remote_node_path_is_posix(client):
    node = next(n for n in client.walk() if n.name == "todo.md")
    assert node.path == PurePosixPath("Notes/todo.md")
    assert not node.is_app_library


def test_walk_depth_stops_descending_rather_than_filtering(drive, client):
    """A bounded walk must not pay for the folders it will not show."""
    listed = [str(node.path) for node in client.walk(depth=1)]
    assert listed == ["top.txt", "Notes"]
    # Descending into Notes at all would have raised, since it cannot list.
    drive.unlistable.add("FOLDER::com.apple.CloudDocs::doc-Notes")
    assert [str(n.path) for n in client.walk(depth=1)] == ["top.txt", "Notes"]


def test_walk_depth_two_reaches_grandchildren(client):
    listed = [str(node.path) for node in client.walk(depth=2)]
    assert listed == ["top.txt", "Notes", "Notes/todo.md", "Notes/Sub"]


def test_walk_skips_excluded_subtrees_without_listing_them(drive, client):
    """An excluded folder must cost no round trip, not merely be filtered out."""
    drive.unlistable.add("FOLDER::com.apple.CloudDocs::doc-Notes")

    listed = [str(n.path) for n in client.walk(skip=lambda p: p.name == "Notes")]

    assert listed == ["top.txt"]
