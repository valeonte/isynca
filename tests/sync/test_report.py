from pathlib import Path

import pytest

from isynca.ledger.store import UploadStatus
from isynca.sync.planner import SkipReason
from isynca.sync.report import RunReport, format_bytes


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1024.0 TiB"),
    ],
)
def test_format_bytes(count, expected):
    assert format_bytes(count) == expected


def test_counts_by_status():
    report = RunReport()
    report.record_status(UploadStatus.CONFIRMED, 100)
    report.record_status(UploadStatus.UNVERIFIED, 200)
    report.record_status(UploadStatus.DUPLICATE, 300)

    assert report.confirmed == 1
    assert report.unverified == 1
    assert report.duplicate == 1
    assert report.uploaded == 3
    assert report.uploaded_bytes == 600


def test_counts_skips_by_reason():
    report = RunReport()
    report.record_skip(SkipReason.ALREADY_UPLOADED)
    report.record_skip(SkipReason.UNREADABLE)

    assert report.skipped_already == 1
    assert report.skipped_unreadable == 1
    assert report.skipped == 2


def test_failures_make_the_run_not_ok():
    report = RunReport()
    assert report.ok
    report.record_failure(Path("/a.mp4"), "boom")
    assert not report.ok
    assert report.failed == 1
    assert report.failures[0].message == "boom"


def test_summary_rows_include_every_metric():
    report = RunReport()
    report.record_status(UploadStatus.CONFIRMED, 2048)
    labels = dict(report.summary_rows())

    assert labels["Uploaded"] == "1"
    assert labels["Bytes sent"] == "2.0 KiB"
    assert "Mode" not in labels


def test_dry_run_is_announced_first():
    rows = RunReport(dry_run=True).summary_rows()
    assert rows[0][0] == "Mode"
    assert "dry run" in rows[0][1]
