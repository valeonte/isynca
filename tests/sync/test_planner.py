from dataclasses import replace
from datetime import UTC, datetime, timedelta

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import UploadStatus
from isynca.media.types import MediaFile, MediaKind
from isynca.sync.planner import (
    PlannedUpload,
    Planner,
    SkippedUpload,
    SkipReason,
    UploadPlan,
)


def test_new_file_is_pending(ledger, make_media):
    media = make_media(content=b"fresh")
    decision = Planner(ledger).evaluate(media)

    assert isinstance(decision, PlannedUpload)
    assert decision.content_hash == hash_file(media.path)
    assert decision.path == media.path
    assert decision.size == media.size


def test_recorded_file_is_skipped(ledger, make_media):
    media = make_media(content=b"seen before")
    digest = hash_file(media.path)
    ledger.record_upload(
        content_hash=digest,
        size=media.size,
        path=media.path,
        status=UploadStatus.CONFIRMED,
    )
    decision = Planner(ledger).evaluate(media)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.ALREADY_UPLOADED
    assert decision.record is not None
    assert decision.record.content_hash == digest


def test_renamed_file_is_recognised_by_content(ledger, tmp_path, make_media):
    """The ledger keys on content, so moving a file must not re-upload it."""
    original = make_media(name="original.mp4", content=b"same bytes")
    ledger.record_upload(
        content_hash=hash_file(original.path),
        size=original.size,
        path=original.path,
        status=UploadStatus.CONFIRMED,
    )
    renamed = make_media(name="renamed.mp4", content=b"same bytes")
    decision = Planner(ledger).evaluate(renamed)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.ALREADY_UPLOADED


def test_unreadable_file_is_skipped(ledger, make_media, monkeypatch):
    media = make_media()
    media.path.unlink()
    decision = Planner(ledger).evaluate(media)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.UNREADABLE
    assert decision.detail


def test_hash_is_cached_between_runs(ledger, make_media, monkeypatch):
    media = make_media(content=b"cache me")
    planner = Planner(ledger)
    assert planner.content_hash(media) == hash_file(media.path)

    def boom(*args, **kwargs):
        raise AssertionError("file should not have been re-hashed")

    monkeypatch.setattr("isynca.sync.planner.hash_file", boom)
    assert planner.content_hash(media) == hash_file(media.path)


def test_cache_is_invalidated_when_content_changes(ledger, make_media):
    media = make_media(content=b"first")
    planner = Planner(ledger)
    first = planner.content_hash(media)

    media.path.write_bytes(b"second version, different length")
    stat = media.path.stat()
    changed = replace(media, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    assert planner.content_hash(changed) != first


def test_plan_splits_pending_and_skipped(ledger, make_media):
    pending = make_media(name="new.mp4", content=b"new content")
    done = make_media(name="old.mp4", content=b"old content")
    ledger.record_upload(
        content_hash=hash_file(done.path),
        size=done.size,
        path=done.path,
        status=UploadStatus.CONFIRMED,
    )

    plan = Planner(ledger).plan([pending, done])
    assert [item.path for item in plan.pending] == [pending.path]
    assert [item.media.path for item in plan.skipped] == [done.path]
    assert plan.total == 2
    assert plan.pending_bytes == pending.size


def test_iter_plan_is_lazy(ledger, make_media):
    media = make_media()
    decisions = Planner(ledger).iter_plan([media])
    first = next(decisions)
    assert isinstance(first, PlannedUpload)
    assert first.path == media.path


def _record_unverified(ledger, media, *, age):
    ledger.record_upload(
        content_hash=hash_file(media.path),
        size=media.size,
        path=media.path,
        status=UploadStatus.UNVERIFIED,
        uploaded_at=datetime.now(UTC) - age,
    )


def test_settled_unverified_upload_is_planned_as_a_recheck(ledger, make_media):
    """ICloud never confirmed it, so sending it again is the only way to ask."""
    media = make_media(content=b"accepted but never named")
    _record_unverified(ledger, media, age=timedelta(days=3))
    decision = Planner(ledger).evaluate(media)

    assert isinstance(decision, PlannedUpload)
    assert decision.is_recheck
    assert decision.previous is not None
    assert decision.previous.status is UploadStatus.UNVERIFIED


def test_fresh_unverified_upload_is_left_to_settle(ledger, make_media):
    """Re-sending while Apple may still be ingesting risks a second copy."""
    media = make_media(content=b"just sent")
    _record_unverified(ledger, media, age=timedelta(minutes=5))
    decision = Planner(ledger).evaluate(media)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.ALREADY_UPLOADED


def test_recheck_waits_for_the_configured_grace(ledger, make_media):
    media = make_media(content=b"sent a while ago")
    _record_unverified(ledger, media, age=timedelta(minutes=5))
    planner = Planner(ledger, recheck_after=timedelta(minutes=1))

    assert isinstance(planner.evaluate(media), PlannedUpload)


def test_confirmed_upload_is_never_rechecked(ledger, make_media):
    media = make_media(content=b"settled long ago")
    ledger.record_upload(
        content_hash=hash_file(media.path),
        size=media.size,
        path=media.path,
        status=UploadStatus.CONFIRMED,
        uploaded_at=datetime.now(UTC) - timedelta(days=30),
    )
    assert isinstance(Planner(ledger).evaluate(media), SkippedUpload)


def test_new_file_is_not_a_recheck(ledger, make_media):
    decision = Planner(ledger).evaluate(make_media(content=b"brand new"))
    assert isinstance(decision, PlannedUpload)
    assert not decision.is_recheck


def test_empty_plan():
    plan = UploadPlan()
    assert plan.total == 0
    assert plan.pending_bytes == 0


# --- capture-date requirement -----------------------------------------------


def test_missing_date_is_skipped_when_required(ledger, make_media):
    media = make_media(content=b"undated")
    planner = Planner(ledger, require_capture_date=True, date_reader=lambda _: None)
    decision = planner.evaluate(media)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.MISSING_DATE


def test_file_with_a_date_is_still_pending(ledger, make_media):
    media = make_media(content=b"dated")
    planner = Planner(
        ledger,
        require_capture_date=True,
        date_reader=lambda _: datetime(2023, 7, 14, tzinfo=UTC),
    )
    assert isinstance(planner.evaluate(media), PlannedUpload)


def test_dates_are_not_checked_unless_required(ledger, make_media):
    def boom(_media):
        raise AssertionError("capture date must not be read unless asked for")

    planner = Planner(ledger, date_reader=boom)
    assert isinstance(planner.evaluate(make_media()), PlannedUpload)


def test_already_uploaded_files_skip_the_date_check(ledger, make_media):
    """Blocking a file iCloud already holds would achieve nothing.

    It would also stop an archive run from filing it away, stranding it in the
    source folder forever.
    """
    media = make_media(content=b"already sent")
    ledger.record_upload(
        content_hash=hash_file(media.path),
        size=media.size,
        path=media.path,
        status=UploadStatus.CONFIRMED,
    )
    planner = Planner(ledger, require_capture_date=True, date_reader=lambda _: None)
    decision = planner.evaluate(media)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.ALREADY_UPLOADED


def test_undated_files_are_split_out_by_plan(ledger, make_media):
    dated = make_media(name="dated.mp4", content=b"has a date")
    undated = make_media(name="undated.mp4", content=b"has none")

    def reader(media):
        return datetime(2023, 1, 1, tzinfo=UTC) if media.name == "dated.mp4" else None

    plan = Planner(ledger, require_capture_date=True, date_reader=reader).plan(
        [dated, undated]
    )
    assert [i.path.name for i in plan.pending] == ["dated.mp4"]
    assert [s.media.path.name for s in plan.skipped] == ["undated.mp4"]


def test_real_files_are_read_by_default(ledger, make_image, make_media):
    """The default reader is the real one, not a stub."""
    path = make_image(name="shot.jpg", original="2023:07:14 12:34:56")
    stat = path.stat()
    media = MediaFile(
        path=path,
        kind=MediaKind.IMAGE,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        source_root=path.parent,
    )
    planner = Planner(ledger, require_capture_date=True)
    assert isinstance(planner.evaluate(media), PlannedUpload)


def test_recheck_is_held_to_the_date_requirement(ledger, make_media):
    """If the first upload never landed, the re-check is what creates the asset."""
    media = make_media(content=b"undated but sent")
    _record_unverified(ledger, media, age=timedelta(days=1))
    planner = Planner(ledger, require_capture_date=True, date_reader=lambda _: None)
    decision = planner.evaluate(media)

    assert isinstance(decision, SkippedUpload)
    assert decision.reason is SkipReason.MISSING_DATE


def test_dated_recheck_passes_the_date_requirement(ledger, make_media):
    media = make_media(content=b"dated and sent")
    _record_unverified(ledger, media, age=timedelta(days=1))
    planner = Planner(
        ledger,
        require_capture_date=True,
        date_reader=lambda _: datetime(2016, 3, 1, tzinfo=UTC),
    )
    decision = planner.evaluate(media)

    assert isinstance(decision, PlannedUpload)
    assert decision.is_recheck
