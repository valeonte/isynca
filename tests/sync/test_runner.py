import errno

import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.errors import AlbumNotFoundError
from isynca.icloud.photos import PhotosUploader
from isynca.ledger.hashing import hash_file
from isynca.ledger.store import UploadStatus
from isynca.retry import RetryPolicy
from isynca.sync.archiver import Archiver
from isynca.sync.planner import PlannedUpload, SkippedUpload, SkipReason, UploadPlan
from isynca.sync.runner import UploadRunner
from tests.fakes.icloud import FakeRegistration, cloudkit_error


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
        FakeRegistration(cplMaster="m1", cplAsset="a1", duplicate=True)
    ]
    media = make_media()
    report = build(session, ledger).run(plan_with(media))

    assert report.duplicate == 1
    assert ledger.lookup(hash_file(media.path)).status is UploadStatus.DUPLICATE


def test_start_hook_fires_before_the_upload(session, ledger, make_media):
    """A bar told only about finished files names the wrong one while it works."""
    events = []
    media = make_media()
    runner = build(
        session,
        ledger,
        on_start=lambda item: events.append(("start", item.path.name)),
        progress=lambda item, status: events.append(("done", item.path.name)),
    )
    runner.run(plan_with(media))

    assert events == [("start", media.path.name), ("done", media.path.name)]


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


def test_a_settled_rejection_is_not_retried(session, ledger, make_media):
    """A refusal of the file itself is an answer, not a bad moment."""
    session.photos_service.upload_results = [
        cloudkit_error(
            "rejected with status 415", payload={"response": {"status": 415}}
        ),
        None,
    ]
    delays = []
    media = make_media()
    report = build(session, ledger, sleep=delays.append).run(plan_with(media))

    assert report.failed == 1
    assert delays == []
    assert session.photos_service.uploaded == [(str(media.path), None)]


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


def test_a_network_blip_is_waited_out_rather_than_failing_the_file(
    session, ledger, make_media
):
    """A socket error escaping the HTTP stack must not fail a good file.

    Six blips is past the ordinary three-attempt budget on purpose: a file
    nothing is wrong with should ride out an outage, not spend its attempts
    on it.
    """
    session.photos_service.upload_results = [
        OSError(errno.EHOSTUNREACH, "No route to host") for _ in range(6)
    ]
    delays = []
    report = build(session, ledger, sleep=delays.append).run(plan_with(make_media()))

    assert report.confirmed == 1
    assert report.failed == 0
    assert len(delays) == 6


def test_a_lasting_outage_still_ends_as_a_recorded_failure(session, ledger, make_media):
    """Waiting is bounded: an unplugged machine must still finish and report."""
    policy = RetryPolicy(attempts=1, offline_attempts=2)
    session.photos_service.upload_results = [
        OSError(errno.ENETUNREACH, "Network is unreachable") for _ in range(2)
    ]
    media = make_media()
    report = build(session, ledger, retry=policy).run(plan_with(media))

    assert report.failed == 1
    assert "Network is unreachable" in report.failures[0].message


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


# --- archiving ---------------------------------------------------------------


def archiving_runner(session, ledger, target, **kwargs):
    kwargs.setdefault("sleep", lambda _: None)
    return UploadRunner(
        uploader=PhotosUploader(session),
        ledger=ledger,
        archiver=Archiver(target, kwargs.pop("dry_run", False)),
        **kwargs,
    )


def test_confirmed_upload_is_moved_to_the_target(session, ledger, tmp_path, make_media):
    target = tmp_path / "archive"
    media = make_media(name="trip/clip.mp4")
    report = archiving_runner(session, ledger, target).run(plan_with(media))

    assert report.moved == 1
    assert (target / "trip" / "clip.mp4").is_file()
    assert not media.path.exists()


def test_duplicate_is_moved_too(session, ledger, tmp_path, make_media):
    """ICloud already holding the content is just as good as uploading it."""
    session.photos_service.upload_results = [
        FakeRegistration(cplMaster="m1", cplAsset="a1", duplicate=True)
    ]
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    report = archiving_runner(session, ledger, target).run(plan_with(media))

    assert report.moved == 1
    assert (target / "clip.mp4").is_file()


def test_unverified_upload_is_held_in_place(session, ledger, tmp_path, make_media):
    session.photos_service.upload_results = [None]
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    report = archiving_runner(session, ledger, target).run(plan_with(media))

    assert report.held_in_place == 1
    assert report.moved == 0
    assert media.path.exists(), "an unconfirmed upload must keep its local copy"


def test_already_uploaded_files_are_moved(session, ledger, tmp_path, make_media):
    """Otherwise an archive run would never drain its source folder."""
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    digest = hash_file(media.path)
    ledger.record_upload(
        content_hash=digest,
        size=media.size,
        path=media.path,
        status=UploadStatus.CONFIRMED,
    )
    plan = plan_with(skipped=[(media, SkipReason.ALREADY_UPLOADED)])
    plan.skipped[0] = SkippedUpload(
        media=media,
        reason=SkipReason.ALREADY_UPLOADED,
        record=ledger.lookup(digest),
    )

    report = archiving_runner(session, ledger, target).run(plan)

    assert report.moved == 1
    assert (target / "clip.mp4").is_file()
    assert session.photos_service.uploaded == [], "nothing needed re-uploading"


def test_already_uploaded_but_unverified_is_held(session, ledger, tmp_path, make_media):
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    digest = hash_file(media.path)
    ledger.record_upload(
        content_hash=digest,
        size=media.size,
        path=media.path,
        status=UploadStatus.UNVERIFIED,
    )
    plan = UploadPlan()
    plan.skipped.append(
        SkippedUpload(
            media=media,
            reason=SkipReason.ALREADY_UPLOADED,
            record=ledger.lookup(digest),
        )
    )

    report = archiving_runner(session, ledger, target).run(plan)

    assert report.held_in_place == 1
    assert media.path.exists()


def test_unreadable_skips_are_not_archived(session, ledger, tmp_path, make_media):
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    plan = plan_with(skipped=[(media, SkipReason.UNREADABLE)])

    report = archiving_runner(session, ledger, target).run(plan)

    assert report.moved == 0
    assert report.held_in_place == 0


def test_skipped_record_without_a_row_is_held(session, ledger, tmp_path, make_media):
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    plan = plan_with(skipped=[(media, SkipReason.ALREADY_UPLOADED)])

    report = archiving_runner(session, ledger, target).run(plan)

    assert report.held_in_place == 1


def test_collision_leaves_the_source_alone(session, ledger, tmp_path, make_media):
    target = tmp_path / "archive"
    target.mkdir()
    (target / "clip.mp4").write_bytes(b"already filed")
    media = make_media(name="clip.mp4")

    report = archiving_runner(session, ledger, target).run(plan_with(media))

    assert report.move_collisions == 1
    assert report.moved == 0
    assert media.path.exists()
    assert (target / "clip.mp4").read_bytes() == b"already filed"


def test_move_failure_is_recorded_and_fails_the_run(
    session, ledger, tmp_path, make_media, monkeypatch
):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("isynca.sync.archiver.shutil.move", boom)
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")

    report = archiving_runner(session, ledger, target).run(plan_with(media))

    assert report.move_failures
    assert not report.ok
    assert report.confirmed == 1, "the upload itself still succeeded"


def test_dry_run_previews_the_move_without_touching_disk(
    session, ledger, tmp_path, make_media
):
    target = tmp_path / "archive"
    media = make_media(name="clip.mp4")
    runner = archiving_runner(session, ledger, target, dry_run=True)

    report = runner.run(plan_with(media))

    assert report.moved == 1
    assert media.path.exists()
    assert not target.exists()


def test_report_marks_itself_as_archiving(session, ledger, tmp_path, make_media):
    report = archiving_runner(session, ledger, tmp_path / "a").run(UploadPlan())
    assert report.archiving


def test_report_is_not_archiving_without_an_archiver(session, ledger, make_media):
    assert not build(session, ledger).run(UploadPlan()).archiving
