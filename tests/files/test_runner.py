from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.errors import ConfigError, DriveListingError, UploadError
from isynca.files.client import DriveClient
from isynca.files.local import LocalScanner
from isynca.files.planner import (
    ActionKind,
    Direction,
    SyncAction,
    SyncPlan,
    SyncPlanner,
)
from isynca.files.runner import (
    SyncRunner,
    check_deletion_threshold,
    remote_index,
)
from isynca.files.state import SyncState
from isynca.sync.runner import RetryPolicy
from tests.fakes.drive import build_drive, document, folder
from tests.fakes.icloud import FakeSession


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "drive"
    path.mkdir()
    return path


@pytest.fixture
def state(tmp_path):
    with SyncState(tmp_path / "drive.db") as store:
        yield store


def sync(client, state, root, *, direction=Direction.BOTH, dry_run=False, **kwargs):
    """Plan and run one full sync, returning the report."""
    local = LocalScanner().scan(root)
    remote = remote_index(client.walk())
    plan = SyncPlanner(direction=direction).plan(local, remote, state.records(root))
    runner = SyncRunner(
        client=client,
        state=state,
        root=root,
        dry_run=dry_run,
        sleep=lambda _s: None,
        **kwargs,
    )
    return runner.run(plan, remote), plan


def client_for(drive):
    return DriveClient(FakeSession(drive_service=drive))


def test_new_local_file_is_uploaded(root, state):
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive()
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert report.uploaded == 1
    assert drive.uploaded[0][1] == "a.txt"
    assert report.bytes_up == 5


def test_upload_state_uses_the_etag_icloud_actually_gave(root, state):
    """Recording a guessed etag would make the next run re-download the file."""
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive()
    client = client_for(drive)

    sync(client, state, root)

    stored = state.records(root)[PurePosixPath("a.txt")]
    assert stored.etag == "server-1"

    # A second run must therefore find nothing to do.
    report, _ = sync(client, state, root)
    assert report.changed == 0
    assert report.unchanged == 1


def test_new_remote_file_is_downloaded(root, state):
    drive = build_drive(document("a.txt", b"remote bytes"))
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert report.downloaded == 1
    assert (root / "a.txt").read_bytes() == b"remote bytes"
    assert state.records(root)[PurePosixPath("a.txt")].etag == "1"


def test_nested_folders_are_created_in_order(root, state):
    (root / "Notes" / "sub").mkdir(parents=True)
    (root / "Notes" / "sub" / "deep.txt").write_bytes(b"deep")
    drive = build_drive()
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert report.created_remote_dirs == 2
    assert [name for _parent, name in drive.created] == ["Notes", "sub"]
    # The file lands in the folder created for it, not at the root.
    assert drive.uploaded[0][0] == "doc-sub"


def test_remote_folders_are_created_locally(root, state):
    drive = build_drive(folder("Notes", document("todo.md", b"todo")))
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert report.created_local_dirs == 1
    assert (root / "Notes" / "todo.md").read_bytes() == b"todo"


def test_local_deletion_trashes_the_remote(root, state):
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive(document("a.txt", b"hello"))
    client = client_for(drive)
    sync(client, state, root)  # adopt

    (root / "a.txt").unlink()
    report, _ = sync(client, state, root)

    assert report.deleted_remote == 1
    assert drive.trashed
    assert state.records(root) == {}


def test_remote_deletion_removes_the_local_file(root, state):
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive(document("a.txt", b"hello"))
    client = client_for(drive)
    sync(client, state, root)

    drive.root_node.children.clear()
    report, _ = sync(client, state, root)

    assert report.deleted_local == 1
    assert not (root / "a.txt").exists()


def test_updating_a_remote_file_trashes_before_uploading(root, state):
    """No overwrite exists in iCloud: uploading onto a live name makes 'a 2.txt'."""
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive(document("a.txt", b"hello"))
    client = client_for(drive)
    sync(client, state, root)

    (root / "a.txt").write_bytes(b"a longer body")
    report, _ = sync(client, state, root)

    assert report.updated_remote == 1
    assert drive.trashed, "the old document must go before the new one arrives"
    assert drive.uploaded[-1][2] == b"a longer body"


def test_remote_edit_overwrites_the_local_copy(root, state):
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive(document("a.txt", b"hello"))
    client = client_for(drive)
    sync(client, state, root)

    drive.root_node.children[0].content = b"edited elsewhere"
    drive.root_node.children[0].etag = "2"
    report, _ = sync(client, state, root)

    assert report.updated_local == 1
    assert (root / "a.txt").read_bytes() == b"edited elsewhere"


def test_local_folder_removal_trashes_the_remote_folder(root, state):
    (root / "Notes").mkdir()
    (root / "Notes" / "a.txt").write_bytes(b"x")
    drive = build_drive(folder("Notes", document("a.txt", b"x")))
    client = client_for(drive)
    sync(client, state, root)

    (root / "Notes" / "a.txt").unlink()
    (root / "Notes").rmdir()
    report, _ = sync(client, state, root)

    assert report.removed_remote_dirs == 1
    # One call, not two: trashing the folder takes its contents with it.
    assert len(drive.trashed) == 1


def test_remote_folder_removal_clears_the_local_folder(root, state):
    (root / "Notes").mkdir()
    (root / "Notes" / "a.txt").write_bytes(b"x")
    drive = build_drive(folder("Notes", document("a.txt", b"x")))
    client = client_for(drive)
    sync(client, state, root)

    drive.root_node.children.clear()
    report, _ = sync(client, state, root)

    assert report.deleted_local == 1
    assert report.removed_local_dirs == 1
    assert not (root / "Notes").exists()


def test_a_local_folder_holding_something_else_survives(root, state):
    """rmdir, never rmtree: an excluded file must not be swept away with it."""
    (root / "Notes").mkdir()
    (root / "Notes" / "a.txt").write_bytes(b"x")
    drive = build_drive(folder("Notes", document("a.txt", b"x")))
    client = client_for(drive)
    sync(client, state, root)

    drive.root_node.children.clear()
    (root / "Notes" / ".DS_Store").write_bytes(b"excluded junk")
    sync(client, state, root)

    assert (root / "Notes").is_dir()
    assert (root / "Notes" / ".DS_Store").exists()


def test_dry_run_changes_nothing(root, state):
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive(document("b.txt", b"remote"))
    client = client_for(drive)

    report, _ = sync(client, state, root, dry_run=True)

    assert report.dry_run
    assert report.uploaded == 1
    assert report.downloaded == 1
    assert drive.uploaded == []
    assert not (root / "b.txt").exists()
    assert state.records(root) == {}


def test_conflicts_are_reported_and_nothing_is_touched(root, state):
    (root / "a.txt").write_bytes(b"local version, longer")
    drive = build_drive(document("a.txt", b"remote"))
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert len(report.conflicts) == 1
    assert not report.ok
    assert drive.uploaded == []
    assert (root / "a.txt").read_bytes() == b"local version, longer"


def test_a_failed_upload_is_recorded_and_the_run_continues(root, state):
    (root / "a.txt").write_bytes(b"one")
    (root / "b.txt").write_bytes(b"two")
    drive = build_drive()
    drive.errors["send_file"] = PyiCloudAPIResponseException("nope", 500)
    client = client_for(drive)

    report, _ = sync(client, state, root, retry=RetryPolicy(attempts=1))

    assert report.failed == 2
    assert not report.ok


def test_a_retryable_failure_is_retried(root, state, monkeypatch):
    (root / "a.txt").write_bytes(b"one")
    drive = build_drive()
    client = client_for(drive)

    attempts = []
    real_upload = client.upload

    def flaky(parent, source):
        attempts.append(source)
        if len(attempts) < 3:
            raise UploadError("transient")
        return real_upload(parent, source)

    monkeypatch.setattr(client, "upload", flaky)
    report, _ = sync(client, state, root)

    assert len(attempts) == 3
    assert report.uploaded == 1


def test_a_settled_failure_is_not_retried(root, state, monkeypatch):
    (root / "a.txt").write_bytes(b"one")
    client = client_for(build_drive())

    attempts = []

    def refused(parent, source):
        attempts.append(source)
        raise UploadError("format refused", retryable=False)

    monkeypatch.setattr(client, "upload", refused)
    report, _ = sync(client, state, root)

    assert len(attempts) == 1
    assert report.failed == 1


def test_an_upload_icloud_will_not_list_is_a_failure(root, state):
    """No state row may be written for a file iCloud cannot confirm holding."""
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive()
    drive.silent_upload = True
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert report.uploaded == 1
    assert report.failed == 1
    assert state.records(root) == {}


def test_push_only_leaves_the_local_side_alone(root, state):
    (root / "a.txt").write_bytes(b"local")
    drive = build_drive(document("remote-only.txt", b"remote"))
    client = client_for(drive)

    report, _ = sync(client, state, root, direction=Direction.PUSH)

    assert report.uploaded == 1
    assert report.downloaded == 0
    assert not (root / "remote-only.txt").exists()
    assert report.suppressed == 1


def test_pull_only_leaves_icloud_alone(root, state):
    (root / "a.txt").write_bytes(b"local")
    drive = build_drive(document("remote-only.txt", b"remote"))
    client = client_for(drive)

    report, _ = sync(client, state, root, direction=Direction.PULL)

    assert report.downloaded == 1
    assert drive.uploaded == []
    assert report.suppressed == 1


def test_adopted_pairs_are_recorded_without_transfer(root, state):
    (root / "a.txt").write_bytes(b"data")
    drive = build_drive(document("a.txt", b"data"))
    client = client_for(drive)

    report, _ = sync(client, state, root)

    assert report.adopted == 1
    assert drive.uploaded == []
    assert PurePosixPath("a.txt") in state.records(root)


def test_stale_rows_are_dropped(root, state):
    (root / "a.txt").write_bytes(b"data")
    drive = build_drive(document("a.txt", b"data"))
    client = client_for(drive)
    sync(client, state, root)

    (root / "a.txt").unlink()
    drive.root_node.children.clear()
    sync(client, state, root)

    assert state.records(root) == {}


def test_a_folder_that_cannot_be_confirmed_leaves_state_unwritten(root, state):
    """The transfer already worked, so a failed re-list must not undo the run."""
    (root / "a.txt").write_bytes(b"hello")
    drive = build_drive()
    client = client_for(drive)

    local = LocalScanner().scan(root)
    remote = remote_index(client.walk())
    plan = SyncPlanner().plan(local, remote, state.records(root))
    runner = SyncRunner(client=client, state=state, root=root, sleep=lambda _s: None)

    original = client.children

    def fail_after_upload(node):
        client.children = original
        raise DriveListingError("iCloud is having a moment")

    client.children = fail_after_upload
    report = runner.run(plan, remote)

    assert report.uploaded == 1
    assert report.ok
    assert state.records(root) == {}


def test_an_upload_into_a_folder_that_failed_to_appear_is_reported(root, state):
    """A file cannot go into a folder the run could not create."""
    (root / "Notes").mkdir()
    (root / "Notes" / "a.txt").write_bytes(b"hello")
    drive = build_drive()
    drive.errors["create_folders"] = PyiCloudAPIResponseException("nope", 500)
    client = client_for(drive)

    report, _ = sync(client, state, root, retry=RetryPolicy(attempts=1))

    assert report.failed == 2
    assert any("No remote folder" in f.message for f in report.failures)


def test_unrelated_actions_survive_a_remote_folder_being_trashed(root, state):
    """Only deletions *inside* the doomed folder are covered by trashing it."""
    (root / "Notes").mkdir()
    (root / "Notes" / "a.txt").write_bytes(b"x")
    drive = build_drive(folder("Notes", document("a.txt", b"x")))
    client = client_for(drive)
    sync(client, state, root)

    (root / "Notes" / "a.txt").unlink()
    (root / "Notes").rmdir()
    (root / "elsewhere.txt").write_bytes(b"new")
    report, _ = sync(client, state, root)

    assert report.removed_remote_dirs == 1
    assert report.uploaded == 1


def test_progress_hook_sees_every_action(root, state):
    (root / "a.txt").write_bytes(b"hello")
    client = client_for(build_drive())
    seen = []

    sync(client, state, root, progress=seen.append)

    assert [action.kind for action in seen] == [ActionKind.UPLOAD]


# --- the deletion rail -------------------------------------------------------


def make_plan_with_deletes(count, root, state):
    plan = SyncPlan()
    plan.actions = [
        SyncAction(ActionKind.DELETE_REMOTE, PurePosixPath(f"f{i}.txt"))
        for i in range(count)
    ]
    return plan


def test_threshold_allows_a_small_run(root, state):
    check_deletion_threshold(make_plan_with_deletes(3, root, state), 50)


def test_threshold_stops_a_bulk_deletion(root, state):
    plan = make_plan_with_deletes(60, root, state)
    with pytest.raises(ConfigError, match="would delete 60 item"):
        check_deletion_threshold(plan, 50)


def test_threshold_names_examples_and_the_way_out(root, state):
    plan = make_plan_with_deletes(60, root, state)
    with pytest.raises(ConfigError, match=r"f0\.txt.*--force"):
        check_deletion_threshold(plan, 50)


def test_force_overrides_the_threshold(root, state):
    check_deletion_threshold(make_plan_with_deletes(60, root, state), 50, force=True)


def test_a_zero_limit_disables_the_rail(root, state):
    check_deletion_threshold(make_plan_with_deletes(60, root, state), 0)


def test_uploading_onto_a_live_name_would_duplicate_it(root, state):
    """The finding that forces trash-then-upload, pinned as a regression.

    Apple's ``allow_conflict`` resolves a name clash by renaming, not by
    versioning. If an update ever stopped trashing first, every edit would
    leave a numbered copy behind instead of replacing the file.
    """
    drive = build_drive(document("a.txt", b"first"))
    client = client_for(drive)
    (root / "a.txt").write_bytes(b"second")

    client.upload(client.root(), root / "a.txt")

    assert sorted(n.name for n in client.children(client.root())) == [
        "a 2.txt",
        "a.txt",
    ]


def test_uploads_are_confirmed_per_folder(root, state):
    """Each folder is re-listed once, and its own files matched against it."""
    (root / "Notes").mkdir()
    (root / "top.txt").write_bytes(b"top")
    (root / "Notes" / "deep.txt").write_bytes(b"deep")
    client = client_for(build_drive())

    report, _ = sync(client, state, root)

    assert report.uploaded == 2
    stored = state.records(root)
    assert stored[PurePosixPath("top.txt")].etag.startswith("server-")
    assert stored[PurePosixPath("Notes/deep.txt")].etag.startswith("server-")
    assert stored[PurePosixPath("top.txt")].etag != (
        stored[PurePosixPath("Notes/deep.txt")].etag
    )
