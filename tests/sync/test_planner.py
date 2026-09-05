from isynca.ledger.hashing import hash_file
from isynca.ledger.store import UploadStatus
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
    changed = type(media)(
        path=media.path, kind=media.kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns
    )
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


def test_empty_plan():
    plan = UploadPlan()
    assert plan.total == 0
    assert plan.pending_bytes == 0
