import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.cli.app import app
from isynca.errors import TwoFactorRequiredError
from isynca.ledger.store import Ledger
from tests.fakes.icloud import FakeSession

ACCOUNT = "tester@example.com"


@pytest.fixture
def fake_icloud(monkeypatch):
    """Wire the CLI's connect() to an in-memory session."""
    fake = FakeSession()
    monkeypatch.setattr("isynca.cli.photos.icloud_session.connect", lambda **k: fake)
    return fake


def uploads(fake):
    return sorted(path.rsplit("/", 1)[-1] for path, _ in fake.photos_service.uploaded)


def test_scan_lists_video_only(invoke, tree):
    result = invoke("photos", "scan", str(tree))
    assert result.exit_code == 0
    assert "a.mp4" in result.output
    assert "b.mov" in result.output
    assert "song.mp3" not in result.output
    assert "2 file(s)" in result.output


def test_scan_can_include_images(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--include-images")
    assert "photo.jpg" in result.output


def test_scan_honours_filters(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--exclude", "*.mov")
    assert "b.mov" not in result.output
    assert "1 file(s)" in result.output


def test_scan_honours_min_size(invoke, tree):
    result = invoke("photos", "scan", str(tree), "--min-size", "150")
    assert "1 file(s)" in result.output


def test_scan_accepts_follow_symlinks(invoke, tree):
    assert invoke("photos", "scan", str(tree), "--follow-symlinks").exit_code == 0


def test_scan_needs_no_network(invoke, tree):
    """The scan path must never authenticate; pytest-socket enforces the rest."""
    assert invoke("photos", "scan", str(tree)).exit_code == 0


def test_upload_sends_every_video(invoke, tree, fake_icloud, data_dir):
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))

    assert result.exit_code == 0
    assert uploads(fake_icloud) == ["a.mp4", "b.mov"]
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.stats()["confirmed"] == 2


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
    assert all(album == "Trip" for _, album in fake_icloud.photos_service.uploaded)
    assert "Trip" in fake_icloud.photos_service.album_container.albums


def test_limit_caps_the_number_of_uploads(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--limit", "1")
    assert len(fake_icloud.photos_service.uploaded) == 1


def test_concurrency_option_is_honoured(invoke, tree, fake_icloud):
    result = invoke(
        "--apple-id", ACCOUNT, "photos", "upload", str(tree), "--concurrency", "2"
    )
    assert result.exit_code == 0
    assert uploads(fake_icloud) == ["a.mp4", "b.mov"]


def test_include_images_uploads_pictures(invoke, tree, fake_icloud):
    invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree), "--include-images")
    assert "photo.jpg" in uploads(fake_icloud)


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
    assert uploads(fake_icloud) == ["a.mp4"]


def test_failures_exit_nonzero_and_are_listed(invoke, tree, fake_icloud):
    fake_icloud.photos_service.upload_results = [
        PyiCloudAPIResponseException("500 boom") for _ in range(6)
    ]
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))

    assert result.exit_code == 1
    assert "Failures" in result.output


def test_unverified_uploads_are_reported(invoke, tree, fake_icloud, data_dir):
    fake_icloud.photos_service.upload_results = [None, None]
    result = invoke("--apple-id", ACCOUNT, "photos", "upload", str(tree))

    assert result.exit_code == 0
    with Ledger(data_dir / "ledger.db") as ledger:
        assert ledger.stats()["unverified"] == 2


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
