from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

from isynca.files.planner import ActionKind, Direction, SyncPlanner
from isynca.files.state import EntryKind, SyncRecord
from tests.files.conftest import (
    dir_record,
    index,
    record_for,
    remote_dir,
    remote_file,
    tree,
)


def plan_for(
    local=None, remote=None, state=None, direction=Direction.BOTH, hasher=None
):
    planner = SyncPlanner(direction=direction, hasher=hasher or (lambda path: "h1"))
    return planner.plan(local or tree(), remote or {}, state or {})


def kinds(plan):
    return {str(action.path): action.kind for action in plan.actions}


def record(local, remote, **overrides):
    """Build a state row for a pair, with fields overridden to stage a change."""
    base = SyncRecord(
        path=local.path,
        kind=EntryKind.FILE,
        size=local.size,
        mtime_ns=local.mtime_ns,
        content_hash="h1",
        etag=remote.etag if remote else "e1",
        remote_size=(remote.size or 0) if remote else 0,
        remote_modified=(
            remote.modified.isoformat() if remote and remote.modified else None
        ),
    )
    return replace(base, **overrides)


# --- files, nothing recorded -------------------------------------------------


def test_local_only_new_file_uploads(make_local):
    local = make_local("a.txt")
    plan = plan_for(tree(files=[local]))
    assert kinds(plan) == {"a.txt": ActionKind.UPLOAD}


def test_remote_only_new_file_downloads():
    plan = plan_for(remote=index(remote_file("a.txt")))
    assert kinds(plan) == {"a.txt": ActionKind.DOWNLOAD}


def test_matching_sizes_are_adopted_not_transferred(make_local):
    local = make_local("a.txt", b"1234")
    plan = plan_for(tree(files=[local]), index(remote_file("a.txt", size=4)))
    assert plan.actions == []
    assert [str(r.path) for r in plan.adopted] == ["a.txt"]
    assert plan.adopted[0].etag == "e1"


def test_differing_sizes_with_no_history_conflict(make_local):
    local = make_local("a.txt", b"12345678")
    plan = plan_for(tree(files=[local]), index(remote_file("a.txt", size=4)))
    assert kinds(plan) == {"a.txt": ActionKind.CONFLICT}
    assert "never synced" in plan.conflicts[0].detail


# --- files, with a recorded agreement ----------------------------------------


def test_untouched_file_is_unchanged(make_local):
    local = make_local("a.txt")
    remote = remote_file("a.txt", size=local.size)
    plan = plan_for(
        tree(files=[local]), index(remote), {local.path: record_for(local, remote)}
    )
    assert plan.actions == []
    assert plan.unchanged == 1


def test_local_edit_updates_remote(make_local):
    local = make_local("a.txt", b"new content")
    remote = remote_file("a.txt", size=4)
    stale = record(local, remote, size=local.size + 1, content_hash="old")
    plan = plan_for(tree(files=[local]), index(remote), {local.path: stale})
    assert kinds(plan) == {"a.txt": ActionKind.UPDATE_REMOTE}


def test_remote_edit_updates_local(make_local):
    local = make_local("a.txt")
    old = remote_file("a.txt", size=local.size, etag="e1")
    new = remote_file("a.txt", size=local.size, etag="e2")
    plan = plan_for(
        tree(files=[local]), index(new), {local.path: record_for(local, old)}
    )
    assert kinds(plan) == {"a.txt": ActionKind.UPDATE_LOCAL}


def test_both_edited_is_a_conflict(make_local):
    local = make_local("a.txt", b"local edit")
    remote = remote_file("a.txt", size=99, etag="e2")
    stale = record(
        local, remote, size=1, mtime_ns=1, content_hash="old", etag="e1", remote_size=1
    )
    plan = plan_for(tree(files=[local]), index(remote), {local.path: stale})
    assert kinds(plan) == {"a.txt": ActionKind.CONFLICT}
    assert "both sides" in plan.conflicts[0].detail


def test_touched_but_unedited_file_is_not_re_uploaded(make_local):
    """A changed mtime with identical bytes must not cost an upload."""
    local = make_local("a.txt", b"same")
    remote = remote_file("a.txt", size=local.size)
    touched = record(local, remote, mtime_ns=local.mtime_ns + 5000)
    plan = plan_for(
        tree(files=[local]), index(remote), {local.path: touched}, hasher=lambda p: "h1"
    )
    assert plan.actions == []
    assert plan.unchanged == 1


# --- deletions ---------------------------------------------------------------


def test_local_deletion_removes_the_remote(make_local):
    local = make_local("a.txt")
    remote = remote_file("a.txt", size=local.size)
    record = record_for(local, remote)
    plan = plan_for(tree(), index(remote), {record.path: record})
    assert kinds(plan) == {"a.txt": ActionKind.DELETE_REMOTE}


def test_remote_deletion_removes_the_local(make_local):
    local = make_local("a.txt")
    remote = remote_file("a.txt", size=local.size)
    record = record_for(local, remote)
    plan = plan_for(tree(files=[local]), {}, {record.path: record})
    assert kinds(plan) == {"a.txt": ActionKind.DELETE_LOCAL}


def test_local_edit_against_a_remote_deletion_is_a_conflict(make_local):
    local = make_local("a.txt", b"edited")
    stale = record(local, None, size=1, mtime_ns=1, content_hash="old")
    plan = plan_for(tree(files=[local]), {}, {stale.path: stale})
    assert kinds(plan) == {"a.txt": ActionKind.CONFLICT}
    assert "deleted in iCloud" in plan.conflicts[0].detail


def test_remote_edit_against_a_local_deletion_is_a_conflict(make_local):
    local = make_local("a.txt")
    record = record_for(local, remote_file("a.txt", size=local.size, etag="e1"))
    changed = remote_file("a.txt", size=local.size, etag="e9")
    plan = plan_for(tree(), index(changed), {record.path: record})
    assert kinds(plan) == {"a.txt": ActionKind.CONFLICT}
    assert "edited in iCloud" in plan.conflicts[0].detail


def test_gone_from_both_sides_drops_the_row(make_local):
    local = make_local("a.txt")
    record = record_for(local, remote_file("a.txt", size=local.size))
    plan = plan_for(tree(), {}, {record.path: record})
    assert plan.actions == []
    assert plan.stale == [PurePosixPath("a.txt")]


# --- folders -----------------------------------------------------------------


def test_new_local_folder_is_created_remotely():
    plan = plan_for(tree(dirs=["Notes"]))
    assert kinds(plan) == {"Notes": ActionKind.MKDIR_REMOTE}


def test_new_remote_folder_is_created_locally():
    plan = plan_for(remote=index(remote_dir("Notes")))
    assert kinds(plan) == {"Notes": ActionKind.MKDIR_LOCAL}


def test_folder_on_both_sides_is_adopted():
    plan = plan_for(tree(dirs=["Notes"]), index(remote_dir("Notes")))
    assert plan.actions == []
    assert [str(r.path) for r in plan.adopted] == ["Notes"]


def test_folder_on_both_sides_with_a_record_is_unchanged():
    plan = plan_for(
        tree(dirs=["Notes"]),
        index(remote_dir("Notes")),
        {PurePosixPath("Notes"): dir_record("Notes")},
    )
    assert plan.actions == []
    assert plan.unchanged == 1


def test_folder_deleted_locally_is_trashed_remotely():
    plan = plan_for(
        tree(),
        index(remote_dir("Notes")),
        {PurePosixPath("Notes"): dir_record("Notes")},
    )
    assert kinds(plan) == {"Notes": ActionKind.RMDIR_REMOTE}


def test_folder_deleted_remotely_is_removed_locally():
    plan = plan_for(
        tree(dirs=["Notes"]), {}, {PurePosixPath("Notes"): dir_record("Notes")}
    )
    assert kinds(plan) == {"Notes": ActionKind.RMDIR_LOCAL}


def test_folder_gone_from_both_sides_drops_the_row():
    plan = plan_for(tree(), {}, {PurePosixPath("Notes"): dir_record("Notes")})
    assert plan.actions == []
    assert plan.stale == [PurePosixPath("Notes")]


def test_trashing_a_remote_folder_covers_its_contents(make_local):
    """Apple trashes recursively, so deleting the children too would only error."""
    local = make_local("Notes/a.txt")
    remote = remote_file("Notes/a.txt", size=local.size)
    state = {
        PurePosixPath("Notes"): dir_record("Notes"),
        local.path: record_for(local, remote),
    }
    plan = plan_for(tree(), index(remote_dir("Notes"), remote), state)
    assert kinds(plan) == {"Notes": ActionKind.RMDIR_REMOTE}


def test_removing_a_local_folder_still_deletes_its_files(make_local):
    """``rmdir`` cannot recurse, so each local file keeps its own delete."""
    local = make_local("Notes/a.txt")
    remote = remote_file("Notes/a.txt", size=local.size)
    state = {
        PurePosixPath("Notes"): dir_record("Notes"),
        local.path: record_for(local, remote),
    }
    plan = plan_for(tree(files=[local], dirs=["Notes"]), {}, state)
    assert kinds(plan) == {
        "Notes": ActionKind.RMDIR_LOCAL,
        "Notes/a.txt": ActionKind.DELETE_LOCAL,
    }


# --- direction ---------------------------------------------------------------


def test_push_only_declines_to_write_locally():
    plan = plan_for(remote=index(remote_file("a.txt")), direction=Direction.PUSH)
    assert plan.actions == []
    assert plan.suppressed == 1


def test_pull_only_declines_to_write_remotely(make_local):
    local = make_local("a.txt")
    plan = plan_for(tree(files=[local]), direction=Direction.PULL)
    assert plan.actions == []
    assert plan.suppressed == 1


def test_push_only_still_deletes_remotely(make_local):
    local = make_local("a.txt")
    remote = remote_file("a.txt", size=local.size)
    record = record_for(local, remote)
    plan = plan_for(tree(), index(remote), {record.path: record}, Direction.PUSH)
    assert kinds(plan) == {"a.txt": ActionKind.DELETE_REMOTE}


def test_unreadable_file_counts_as_changed(make_local):
    """A file that cannot be hashed must not be mistaken for an unchanged one."""
    local = make_local("a.txt")
    remote = remote_file("a.txt", size=local.size)
    stale = record(local, remote, size=local.size + 1)

    def boom(path):
        raise OSError("permission denied")

    plan = plan_for(
        tree(files=[local]), index(remote), {local.path: stale}, hasher=boom
    )
    assert kinds(plan) == {"a.txt": ActionKind.UPDATE_REMOTE}


def test_plan_helpers_group_actions(make_local):
    local = make_local("a.txt")
    plan = plan_for(tree(files=[local], dirs=["Notes"]))
    assert len(plan.transfers) == 1
    assert not plan.empty
    assert plan.deletions == []
    assert plan.of(ActionKind.MKDIR_REMOTE)[0].depth == 1
