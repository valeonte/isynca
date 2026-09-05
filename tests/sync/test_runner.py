import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.errors import AlbumNotFoundError
from isynca.icloud.photos import PhotosUploader
from isynca.ledger.hashing import hash_file
from isynca.ledger.store import UploadStatus
from isynca.sync.planner import PlannedUpload, SkippedUpload, SkipReason, UploadPlan
from isynca.sync.runner import RetryPolicy, UploadRunner


def plan_with(*media, skipped=()):
    plan = UploadPlan()
    for item in media:
        plan.pending.append(
            PlannedUpload(media=item, content_hash=hash_file(item.path))
        )
    for item, reason in skipped:
        plan.skipped.append(SkippedUpload(media=item, reason=reason))
    return plan


def build(session, ledger, **kwargs):
    kwargs.setdefault("sleep", lambda _: None)
    return UploadRunner(uploader=PhotosUploader(session), ledger=ledger, **kwargs)


def test_successful_run_records_everything(session, ledger, make_media):
    media = make_media(content=b"payload")
    report = build(session, ledger).run(plan_with(media))

    assert report.confirmed == 1
    assert report.uploaded_bytes == media.size
    assert report.ok

    stored = ledger.lookup(hash_file(media.path))
    assert stored.status is UploadStatus.CONFIRMED
    assert stored.asset_id == "asset-1"
    assert stored.first_path == media.path


def test_unverified_result_is_still_recorded(session, ledger, make_media):
    """An un-indexed upload must be remembered, or the next run re-sends it."""
    session.photos_service.upload_results = [None]
    media = make_media()
    report = build(session, ledger).run(plan_with(media))

    assert report.unverified == 1
    assert ledger.lookup(hash_file(media.path)).status is UploadStatus.UNVERIFIED


def test_duplicate_result_is_recorded(session, ledger, make_media):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("duplicate asset")
    ]
    media = make_media()
    report = build(session, ledger).run(plan_with(media))

    assert report.duplicate == 1
    assert ledger.lookup(hash_file(media.path)).status is UploadStatus.DUPLICATE


def test_skipped_items_are_counted(session, ledger, make_media):
    media = make_media()
    plan = plan_with(skipped=[(media, SkipReason.ALREADY_UPLOADED)])
    report = build(session, ledger).run(plan)

    assert report.skipped_already == 1
    assert session.photos_service.uploaded == []


def test_dry_run_uploads_nothing_and_records_nothing(session, ledger, make_media):
    media = make_media()
    report = build(session, ledger, dry_run=True).run(plan_with(media))

    assert report.dry_run
    assert report.uploaded == 0
    assert session.photos_service.uploaded == []
    assert ledger.lookup(hash_file(media.path)) is None


def test_dry_run_needs_no_uploader(ledger, make_media):
    runner = UploadRunner(uploader=None, ledger=ledger, dry_run=True)
    assert runner.run(plan_with(make_media())).uploaded == 0


def test_transient_failure_is_retried_then_succeeds(session, ledger, make_media):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("500 flaky"),
        None,
    ]
    delays = []
    runner = build(session, ledger, sleep=delays.append)
    report = runner.run(plan_with(make_media()))

    assert report.unverified == 1
    assert report.ok
    assert delays == [1.0]


def test_failure_is_recorded_after_attempts_are_exhausted(session, ledger, make_media):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("500 down") for _ in range(3)
    ]
    media = make_media()
    report = build(session, ledger).run(plan_with(media))

    assert report.failed == 1
    assert not report.ok
    assert report.failures[0].path == media.path
    assert ledger.lookup(hash_file(media.path)) is None


def test_a_failure_does_not_stop_later_files(session, ledger, make_media):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("500 down"),
        PyiCloudAPIResponseException("500 down"),
        PyiCloudAPIResponseException("500 down"),
    ]
    bad = make_media(name="bad.mp4", content=b"bad")
    good = make_media(name="good.mp4", content=b"good")
    report = build(session, ledger).run(plan_with(bad, good))

    assert report.failed == 1
    assert report.confirmed == 1


def test_fatal_errors_are_not_retried(session, ledger, make_media):
    """A fatal error aborts the run rather than being retried per file.

    Album resolution is cached after its first attempt, so a retry would sail
    past the failure and actually upload; an empty upload list is therefore
    proof that no retry happened.
    """
    session.photos_service.create_returns_none = True
    runner = UploadRunner(
        uploader=PhotosUploader(session, album="Gone"),
        ledger=ledger,
        sleep=lambda _: None,
    )

    with pytest.raises(AlbumNotFoundError):
        runner.run(plan_with(make_media()))
    assert session.photos_service.uploaded == []


def test_retry_policy_backs_off_exponentially():
    policy = RetryPolicy(attempts=4, initial_delay=2.0, backoff=3.0)
    assert [policy.delay_for(n) for n in (1, 2, 3)] == [2.0, 6.0, 18.0]


def test_progress_hook_is_called_per_file(session, ledger, make_media):
    seen = []
    runner = build(session, ledger, progress=lambda item, status: seen.append(status))
    runner.run(plan_with(make_media(name="a.mp4"), make_media(name="b.mp4")))

    assert seen == [UploadStatus.CONFIRMED, UploadStatus.CONFIRMED]


def test_progress_hook_reports_failures_as_none(session, ledger, make_media):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("500") for _ in range(3)
    ]
    seen = []
    runner = build(session, ledger, progress=lambda item, status: seen.append(status))
    runner.run(plan_with(make_media()))
    assert seen == [None]


def test_files_upload_one_at_a_time_in_plan_order(session, ledger, make_media):
    media = [
        make_media(name=f"clip{i}.mp4", content=f"body-{i}".encode()) for i in range(8)
    ]
    report = build(session, ledger).run(plan_with(*media))

    assert report.confirmed == 8
    uploaded = [path for path, _ in session.photos_service.uploaded]
    assert uploaded == [str(item.path) for item in media]
    for item in media:
        assert ledger.lookup(hash_file(item.path)) is not None


def test_empty_plan_produces_an_empty_report(session, ledger):
    report = build(session, ledger).run(UploadPlan())
    assert report.uploaded == 0
    assert report.ok
