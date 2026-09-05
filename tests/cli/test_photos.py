import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.cli.app import app
from isynca.errors import TwoFactorRequiredError
from isynca.ledger.store import Ledger
from tests.fakes.icloud import FakeSession
from tests.media.conftest import box, mvhd

ACCOUNT = "tester@example.com"


@pytest.fixture
def fake_icloud(monkeypatch):
    """Wire the CLI's connect() to an in-memory session."""
    fake = FakeSession()
    monkeypatch.setattr("isynca.cli.photos.icloud_session.connect", lambda **k: fake)
    return fake


def uploads(fake):
    return sorted(path.rsplit("/", 1)[-1] for path, _ in fake.photos_service.uploaded)


def test_scan_lists_video_and_images_by_default(invoke, tree):
    result = invoke("photos", "scan", str(tree))
    assert result.exit_code == 0
    assert "a.mp4" in result.output
    assert "b.mov" in result.output
    assert "photo.jpg" in result.output
    assert "song.mp3" not in result.output
    assert "3 file(s)" in result.output


def test_scan_can_exclude_images(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--no-images")
    assert "photo.jpg" not in result.output
    assert "a.mp4" in result.output
    assert "2 file(s)" in result.output


def test_scan_can_exclude_videos(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--no-videos")
    assert "a.mp4" not in result.output
    assert "b.mov" not in result.output
    assert "photo.jpg" in result.output
    assert "1 file(s)" in result.output


def test_scan_kinds_can_be_re_enabled_explicitly(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--videos", "--images")
    assert "3 file(s)" in result.output


def test_excluding_both_kinds_is_an_error(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--no-videos", "--no-images")
    assert result.exit_code != 0


def test_scan_honours_filters(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--exclude", "*.mov")
    assert "b.mov" not in result.output
    assert "2 file(s)" in result.output


def test_scan_honours_min_size(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--min-size", "150")
    assert "1 file(s)" in result.output


def test_scan_accepts_follow_symlinks(invoke, tree):
    assert invoke("photos", "scan", str(tree), "--follow-symlinks").exit_code == 0


def test_scan_needs_no_network(invoke, tree):
    """The scan path must never authenticate; pytest-socket enforces the rest."""
    assert invoke("photos", "scan", str(tree)).exit_code == 0


def test_upload_sends_video_and_images(invoke, tree, fake_icloud, data_dir):
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))

    assert result.exit_code == 0
    assert uploads(fake_icloud) == ["a.mp4", "b.mov", "photo.jpg"]
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.stats()["confirmed"] == 3


def test_second_run_skips_everything(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))
    fake_icloud.photos_service.uploaded.clear()

    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))
    assert result.exit_code == 0
    assert "up to date" in result.output
    assert fake_icloud.photos_service.uploaded == []


def test_dry_run_uploads_nothing(invoke, tree, fake_icloud, data_dir):
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert fake_icloud.photos_service.uploaded == []
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.stats()["total"] == 0


def test_dry_run_needs_no_credentials(invoke, tree, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("dry run must not authenticate")

    monkeypatch.setattr("isynca.cli.photos.icloud_session.connect", boom)
    assert invoke("photos", "upload", str(tree), "--dry-run").exit_code == 0


def test_upload_into_an_album(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--album", "Trip")
    albums = fake_icloud.photos_service.album_container.albums
    assert "Trip" in albums
    assert len(albums["Trip"].added) == len(fake_icloud.photos_service.uploaded) == 3


def test_limit_caps_the_number_of_uploads(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--limit", "1")
    assert len(fake_icloud.photos_service.uploaded) == 1


def test_upload_can_exclude_images(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--no-images")
    assert uploads(fake_icloud) == ["a.mp4", "b.mov"]


def test_upload_can_exclude_videos(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--no-videos")
    assert uploads(fake_icloud) == ["photo.jpg"]


def test_upload_excluding_both_kinds_is_an_error(invoke, tree, fake_icloud):
    result = invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "upload",
        str(tree),
        "--no-videos",
        "--no-images",
    )
    assert result.exit_code != 0
    assert fake_icloud.photos_service.uploaded == []


def test_filters_apply_to_upload(invoke, tree, fake_icloud):
    invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "upload",
        str(tree),
        "--exclude",
        "*.mov",
        "--min-size",
        "10",
        "--follow-symlinks",
    )
    assert uploads(fake_icloud) == ["a.mp4", "photo.jpg"]


def test_failures_exit_nonzero_and_are_listed(invoke, tree, fake_icloud):
    fake_icloud.photos_service.upload_results = [
        PyiCloudAPIResponseException("500 boom") for _ in range(9)
    ]
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))

    assert result.exit_code == 1
    assert "Failures" in result.output


def test_unverified_uploads_are_reported(invoke, tree, fake_icloud, data_dir):
    fake_icloud.photos_service.upload_results = [None, None, None]
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))

    assert result.exit_code == 0
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.stats()["unverified"] == 3


def test_upload_requires_an_apple_id(invoke, tree, fake_icloud):
    assert invoke("photos", "upload", str(tree)).exit_code != 0


def test_upload_surfaces_a_2fa_requirement(invoke, tree, monkeypatch):
    def boom(**kwargs):
        raise TwoFactorRequiredError("run isynca auth login")

    monkeypatch.setattr("isynca.cli.photos.icloud_session.connect", boom)
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))
    assert result.exit_code != 0


def test_empty_source_reports_nothing_to_do(invoke, tmp_path, fake_icloud):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(empty))

    assert result.exit_code == 0
    assert "up to date" in result.output


def test_skipped_files_appear_in_the_summary(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))
    assert "Skipped (in ledger)" in result.output


def test_photos_group_help(runner):
    result = runner.invoke(app, ["photos", "--help"])
    assert result.exit_code == 0
    assert "scan" in result.output
    assert "upload" in result.output


# --- archive mode ------------------------------------------------------------


def test_archive_uploads_then_moves_preserving_structure(
    invoke, tree, fake_icloud, tmp_path
):
    target = tmp_path / "archive"
    result = invoke(
        "--apple-id", ACCOUNT, "photos", "archive", str(tree), "--to", str(target)
    )

    assert result.exit_code == 0
    assert uploads(fake_icloud) == ["a.mp4", "b.mov", "photo.jpg"]
    assert (target / "trip" / "a.mp4").is_file()
    assert (target / "trip" / "day1" / "b.mov").is_file()
    assert (target / "trip" / "photo.jpg").is_file()
    # The source keeps only what was never eligible.
    assert not (tree / "trip" / "a.mp4").exists()
    assert (tree / "trip" / "song.mp3").exists(), "audio is untouched"


def test_archive_drains_previously_uploaded_files(invoke, tree, fake_icloud, tmp_path):
    """A second run has nothing to upload but must still file the leftovers."""
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))
    fake_icloud.photos_service.uploaded.clear()

    target = tmp_path / "archive"
    result = invoke(
        "--apple-id", ACCOUNT, "photos", "archive", str(tree), "--to", str(target)
    )

    assert result.exit_code == 0
    assert fake_icloud.photos_service.uploaded == [], "nothing to re-upload"
    assert (target / "trip" / "a.mp4").is_file()
    assert not (tree / "trip" / "a.mp4").exists()


def test_archive_holds_unverified_uploads(invoke, tree, fake_icloud, tmp_path):
    fake_icloud.photos_service.upload_results = [None, None, None]
    target = tmp_path / "archive"
    result = invoke(
        "--apple-id", ACCOUNT, "photos", "archive", str(tree), "--to", str(target)
    )

    assert result.exit_code == 0
    assert not target.exists()
    assert (tree / "trip" / "a.mp4").exists()
    assert "Held (iCloud not confirmed)" in result.output


def test_archive_does_not_overwrite_at_the_target(invoke, tree, fake_icloud, tmp_path):
    target = tmp_path / "archive"
    (target / "trip").mkdir(parents=True)
    (target / "trip" / "a.mp4").write_bytes(b"already filed")

    result = invoke(
        "--apple-id", ACCOUNT, "photos", "archive", str(tree), "--to", str(target)
    )

    assert result.exit_code == 0
    assert (target / "trip" / "a.mp4").read_bytes() == b"already filed"
    assert (tree / "trip" / "a.mp4").exists()
    assert "Not moved (already at target)" in result.output


def test_archive_dry_run_changes_nothing(invoke, tree, fake_icloud, tmp_path):
    target = tmp_path / "archive"
    result = invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "archive",
        str(tree),
        "--to",
        str(target),
        "--dry-run",
    )

    assert result.exit_code == 0
    assert fake_icloud.photos_service.uploaded == []
    assert not target.exists()
    assert (tree / "trip" / "a.mp4").exists()
    assert "dry run" in result.output


def test_archive_rejects_a_target_inside_the_source(invoke, tree, fake_icloud):
    result = invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "archive",
        str(tree),
        "--to",
        str(tree / "done"),
    )
    assert result.exit_code != 0
    assert fake_icloud.photos_service.uploaded == []


def test_archive_honours_kind_filters(invoke, tree, fake_icloud, tmp_path):
    target = tmp_path / "archive"
    invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "archive",
        str(tree),
        "--to",
        str(target),
        "--no-images",
    )
    assert not (target / "trip" / "photo.jpg").exists()
    assert (target / "trip" / "a.mp4").is_file()


def test_archive_reports_move_failures(
    invoke, tree, fake_icloud, tmp_path, monkeypatch
):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("isynca.sync.archiver.shutil.move", boom)
    target = tmp_path / "archive"
    result = invoke(
        "--apple-id", ACCOUNT, "photos", "archive", str(tree), "--to", str(target)
    )

    assert result.exit_code == 1
    assert "Failures" in result.output


def test_archive_of_a_single_file_source(invoke, tree, fake_icloud, tmp_path):
    target = tmp_path / "archive"
    clip = tree / "trip" / "a.mp4"
    invoke("--apple-id", ACCOUNT, "photos", "archive", str(clip), "--to", str(target))

    assert (target / "a.mp4").is_file()


def test_archive_appears_in_help(runner):
    result = runner.invoke(app, ["photos", "--help"])
    assert "archive" in result.output


# --- capture-date requirement -----------------------------------------------


@pytest.fixture
def dated_tree(tmp_path, make_image, make_video):
    """A source tree where exactly one photo and one video lack a date."""
    root = tmp_path / "media"
    (root / "trip").mkdir(parents=True)
    for src, name in (
        (make_image(name="a.jpg", original="2023:07:14 12:34:56"), "trip/a.jpg"),
        (make_image(name="b.jpg"), "trip/b.jpg"),
        (make_video(name="c.mp4"), "trip/c.mp4"),
        (make_video(name="d.mp4", moov=box(b"moov", mvhd(0))), "trip/d.mp4"),
    ):
        src.rename(root / name)
    return root


def test_undated_files_are_not_uploaded(invoke, dated_tree, fake_icloud):
    result = invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "upload",
        str(dated_tree),
        "--require-date-taken",
    )
    assert result.exit_code == 0
    assert uploads(fake_icloud) == ["a.jpg", "c.mp4"]


def test_undated_files_are_listed_by_name(invoke, dated_tree, fake_icloud):
    """A count is not actionable; the point is knowing which files."""
    result = invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "upload",
        str(dated_tree),
        "--require-date-taken",
    )
    assert "No capture date" in result.output
    assert "b.jpg" in result.output
    assert "d.mp4" in result.output
    assert "Skipped (no capture date)" in result.output


def test_everything_uploads_without_the_switch(invoke, dated_tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(dated_tree))
    assert uploads(fake_icloud) == ["a.jpg", "b.jpg", "c.mp4", "d.mp4"]


def test_undated_files_are_not_archived(invoke, dated_tree, fake_icloud, tmp_path):
    """Held-back files must stay in the source, not be filed away."""
    target = tmp_path / "archive"
    result = invoke(
        "--apple-id",
        ACCOUNT,
        "photos",
        "archive",
        str(dated_tree),
        "--to",
        str(target),
        "--require-date-taken",
    )
    assert result.exit_code == 0
    assert (target / "trip" / "a.jpg").is_file()
    assert not (target / "trip" / "b.jpg").exists()
    assert (dated_tree / "trip" / "b.jpg").exists()


def test_scan_shows_capture_dates(invoke, dated_tree):
    result = invoke("photos", "scan", str(dated_tree), "--require-date-taken")
    assert result.exit_code == 0
    assert "Date taken" in result.output
    assert "2023-07-14" in result.output
    assert "missing" in result.output
    assert "2 file(s) with no capture date" in result.output


def test_scan_omits_the_date_column_by_default(invoke, dated_tree):
    result = invoke("photos", "scan", str(dated_tree))
    assert "Date taken" not in result.output


def test_scan_needs_no_network_for_dates(invoke, dated_tree):
    """pytest-socket enforces this; the assertion documents the intent."""
    assert (
        invoke("photos", "scan", str(dated_tree), "--require-date-taken").exit_code == 0
    )
