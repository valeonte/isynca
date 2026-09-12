from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from isynca.files.planner import ActionKind, SyncAction
from isynca.files.report import SyncReport
from tests.files.conftest import remote_file


def action(kind, path="a.txt", local=None, remote=None, detail=""):
    return SyncAction(
        kind=kind, path=PurePosixPath(path), local=local, remote=remote, detail=detail
    )


def labels(report):
    return dict(report.summary_rows())


def test_a_fresh_report_is_ok():
    report = SyncReport()
    assert report.ok
    assert report.changed == 0
    assert report.failed == 0


@pytest.mark.parametrize(
    ("kind", "attribute"),
    [
        (ActionKind.UPLOAD, "uploaded"),
        (ActionKind.UPDATE_REMOTE, "updated_remote"),
        (ActionKind.DOWNLOAD, "downloaded"),
        (ActionKind.UPDATE_LOCAL, "updated_local"),
        (ActionKind.DELETE_REMOTE, "deleted_remote"),
        (ActionKind.DELETE_LOCAL, "deleted_local"),
        (ActionKind.MKDIR_REMOTE, "created_remote_dirs"),
        (ActionKind.MKDIR_LOCAL, "created_local_dirs"),
        (ActionKind.RMDIR_REMOTE, "removed_remote_dirs"),
        (ActionKind.RMDIR_LOCAL, "removed_local_dirs"),
    ],
)
def test_every_action_kind_is_counted(kind, attribute):
    report = SyncReport()
    report.record(action(kind))
    assert getattr(report, attribute) == 1
    assert report.changed == 1


def test_bytes_are_attributed_to_the_right_direction(make_local):
    report = SyncReport()
    local = make_local("a.txt", b"12345")
    report.record(action(ActionKind.UPLOAD, local=local))
    report.record(action(ActionKind.DOWNLOAD, remote=remote_file("b.txt", size=7)))
    assert report.bytes_up == 5
    assert report.bytes_down == 7


def test_transfers_without_a_side_count_no_bytes():
    report = SyncReport()
    report.record(action(ActionKind.UPLOAD))
    report.record(action(ActionKind.DOWNLOAD))
    assert report.bytes_up == 0
    assert report.bytes_down == 0


def test_a_conflict_makes_the_run_not_ok():
    report = SyncReport()
    report.record_conflict(action(ActionKind.CONFLICT, detail="both sides"))
    assert not report.ok
    assert labels(report)["Conflicts"] == "1"


def test_a_failure_makes_the_run_not_ok():
    report = SyncReport()
    report.record_failure(PurePosixPath("a.txt"), "boom")
    assert not report.ok
    assert report.failed == 1
    assert report.failures[0].path == PurePosixPath("a.txt")


def test_folder_rows_appear_only_when_folders_moved():
    quiet = SyncReport()
    assert "Folders created (iCloud)" not in labels(quiet)

    busy = SyncReport()
    busy.record(action(ActionKind.MKDIR_REMOTE, "Notes"))
    assert labels(busy)["Folders created (iCloud)"] == "1"


def test_adopted_and_suppressed_rows_appear_only_when_relevant():
    plain = SyncReport()
    assert "Matched, now tracked" not in labels(plain)

    noted = SyncReport(adopted=3, suppressed=2, direction="push")
    rows = labels(noted)
    assert rows["Matched, now tracked"] == "3"
    assert rows["Ignored (push only)"] == "2"


def test_a_dry_run_says_so_first():
    rows = SyncReport(dry_run=True).summary_rows()
    assert rows[0] == ("Mode", "dry run - nothing was changed")


def test_byte_counts_are_human_readable():
    report = SyncReport(bytes_up=2048, bytes_down=0)
    assert labels(report)["Bytes sent"] == "2.0 KiB"
    assert labels(report)["Bytes received"] == "0 B"
