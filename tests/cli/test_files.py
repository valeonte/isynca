from __future__ import annotations

import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.errors import ConfigError, TwoFactorRequiredError
from tests.fakes.drive import APP_LIBRARY, FakeNode, build_drive, document, folder
from tests.fakes.icloud import FakeSession

ACCOUNT = "tester@example.com"


@pytest.fixture
def drive():
    return build_drive(
        document("top.txt", b"top"),
        folder("Notes", document("todo.md", b"todo")),
        FakeNode("doc-pages", "com.apple.Pages", node_type=APP_LIBRARY),
    )


@pytest.fixture
def fake_icloud(monkeypatch, drive):
    """Wire the CLI's connect() to an in-memory session."""
    session = FakeSession(drive_service=drive)
    monkeypatch.setattr("isynca.cli.files.icloud_session.connect", lambda **k: session)
    return session


def test_list_shows_the_root_by_default(invoke, fake_icloud):
    result = invoke("--apple-id", ACCOUNT, "files", "list")
    assert result.exit_code == 0
    assert "top.txt" in result.output
    assert "Notes" in result.output
    # Depth 1 stops at the root's own children.
    assert "todo.md" not in result.output
    assert "1 file(s)" in result.output


def test_list_descends_when_depth_allows(invoke, fake_icloud):
    result = invoke("--apple-id", ACCOUNT, "files", "list", "--depth", "0")
    assert result.exit_code == 0
    assert "Notes/todo.md" in result.output
    assert "2 file(s)" in result.output


def test_list_hides_app_libraries_unless_asked(invoke, fake_icloud):
    assert (
        "com.apple.Pages" not in invoke("--apple-id", ACCOUNT, "files", "list").stdout
    )
    shown = invoke(
        "--apple-id", ACCOUNT, "files", "list", "--include-app-libraries"
    ).stdout
    assert "com.apple.Pages" in shown
    assert APP_LIBRARY.lower() in shown


def test_list_reports_the_raw_node_type(invoke, fake_icloud):
    result = invoke("--apple-id", ACCOUNT, "files", "list")
    assert "file" in result.output
    assert "folder" in result.output


def test_put_uploads_into_the_root(invoke, fake_icloud, drive, tmp_path):
    source = tmp_path / "hello.txt"
    source.write_bytes(b"hi")

    result = invoke("--apple-id", ACCOUNT, "files", "put", str(source))

    assert result.exit_code == 0
    assert drive.uploaded[0][0] == "root"
    assert drive.uploaded[0][1] == "hello.txt"


def test_put_uploads_into_a_named_folder(invoke, fake_icloud, drive, tmp_path):
    source = tmp_path / "hello.txt"
    source.write_bytes(b"hi")

    result = invoke("--apple-id", ACCOUNT, "files", "put", str(source), "--to", "Notes")

    assert result.exit_code == 0
    assert drive.uploaded[0][0] == "doc-Notes"


def test_put_rejects_an_unknown_folder(invoke, fake_icloud, tmp_path):
    source = tmp_path / "hello.txt"
    source.write_bytes(b"hi")
    result = invoke(
        "--apple-id", ACCOUNT, "files", "put", str(source), "--to", "Nowhere"
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert "No such folder" in str(result.exception)


def test_put_rejects_a_missing_source(invoke, fake_icloud, tmp_path):
    result = invoke("--apple-id", ACCOUNT, "files", "put", str(tmp_path / "absent.txt"))
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert "Not a file" in str(result.exception)


def test_files_commands_surface_a_2fa_requirement(invoke, monkeypatch):
    def boom(**_kwargs):
        raise TwoFactorRequiredError("run auth login")

    monkeypatch.setattr("isynca.cli.files.icloud_session.connect", boom)
    result = invoke("--apple-id", ACCOUNT, "files", "list")
    assert result.exit_code != 0
    assert isinstance(result.exception, TwoFactorRequiredError)


# --- sync --------------------------------------------------------------------


@pytest.fixture
def local_root(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    return root


def sync_args(root, *extra):
    return ("--apple-id", ACCOUNT, "files", "sync", str(root), *extra)


def test_sync_uploads_and_downloads(invoke, fake_icloud, drive, local_root):
    (local_root / "mine.txt").write_bytes(b"mine")

    result = invoke(*sync_args(local_root))

    assert result.exit_code == 0
    assert drive.uploaded[0][1] == "mine.txt"
    assert (local_root / "top.txt").read_bytes() == b"top"
    assert (local_root / "Notes" / "todo.md").read_bytes() == b"todo"


def test_sync_is_idempotent(invoke, fake_icloud, drive, local_root):
    invoke(*sync_args(local_root))
    before = len(drive.uploaded)

    result = invoke(*sync_args(local_root))

    assert result.exit_code == 0
    assert len(drive.uploaded) == before
    assert "Unchanged" in result.output


def test_sync_dry_run_changes_nothing(invoke, fake_icloud, drive, local_root):
    (local_root / "mine.txt").write_bytes(b"mine")

    result = invoke(*sync_args(local_root, "--dry-run"))

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert drive.uploaded == []
    assert not (local_root / "top.txt").exists()


def test_sync_deletes_remotely_when_deleted_locally(
    invoke, fake_icloud, drive, local_root
):
    invoke(*sync_args(local_root))
    (local_root / "top.txt").unlink()

    result = invoke(*sync_args(local_root))

    assert result.exit_code == 0
    assert drive.trashed
    assert "Deleted from iCloud" in result.output


def test_sync_reports_conflicts_and_exits_nonzero(
    invoke, fake_icloud, drive, local_root
):
    (local_root / "top.txt").write_bytes(b"a different length entirely")

    result = invoke(*sync_args(local_root))

    assert result.exit_code == 1
    assert "Conflicts" in result.output
    assert "top.txt" in result.output


def test_sync_refuses_a_bulk_deletion(invoke, fake_icloud, drive, local_root):
    invoke(*sync_args(local_root))
    for name in ("top.txt",):
        (local_root / name).unlink()
    (local_root / "Notes" / "todo.md").unlink()

    result = invoke(*sync_args(local_root, "--max-deletes", "1"))

    assert result.exit_code != 0
    assert "would delete" in str(result.exception)


def test_force_gets_past_the_deletion_limit(invoke, fake_icloud, drive, local_root):
    invoke(*sync_args(local_root))
    (local_root / "top.txt").unlink()
    (local_root / "Notes" / "todo.md").unlink()

    result = invoke(*sync_args(local_root, "--max-deletes", "1", "--force"))

    assert result.exit_code == 0


def test_push_only_ignores_remote_files(invoke, fake_icloud, drive, local_root):
    (local_root / "mine.txt").write_bytes(b"mine")

    result = invoke(*sync_args(local_root, "--push-only"))

    assert result.exit_code == 0
    assert not (local_root / "top.txt").exists()
    assert drive.uploaded[0][1] == "mine.txt"


def test_pull_only_ignores_local_files(invoke, fake_icloud, drive, local_root):
    (local_root / "mine.txt").write_bytes(b"mine")

    result = invoke(*sync_args(local_root, "--pull-only"))

    assert result.exit_code == 0
    assert drive.uploaded == []
    assert (local_root / "top.txt").exists()


def test_opposing_direction_flags_are_rejected(invoke, fake_icloud, local_root):
    result = invoke(*sync_args(local_root, "--push-only", "--pull-only"))
    assert result.exit_code != 0
    assert "cancel out" in str(result.exception)


def test_sync_rejects_a_path_that_is_not_a_folder(invoke, fake_icloud, tmp_path):
    target = tmp_path / "a-file"
    target.write_text("hi")
    result = invoke("--apple-id", ACCOUNT, "files", "sync", str(target))
    assert result.exit_code != 0
    assert "Not a folder" in str(result.exception)


def test_sync_honours_exclude_globs(invoke, fake_icloud, drive, local_root):
    (local_root / "keep.txt").write_bytes(b"keep")
    (local_root / "skip.tmp").write_bytes(b"skip")

    invoke(*sync_args(local_root, "--exclude", "*.tmp"))

    assert sorted(name for _f, name, _c, _k in drive.uploaded) == ["keep.txt"]


def test_sync_reports_failures(invoke, fake_icloud, drive, local_root):
    (local_root / "mine.txt").write_bytes(b"mine")
    drive.errors["send_file"] = PyiCloudAPIResponseException("nope", 500)

    result = invoke(*sync_args(local_root))

    assert result.exit_code == 1
    assert "Failures" in result.output


def test_sync_excludes_apply_to_the_remote_side_too(
    invoke, fake_icloud, drive, local_root
):
    """Junk skipped on the way up must not be recreated on the way down."""
    drive.root_node.children.append(folder(".com-apple-bird-noname-ABC"))

    invoke(*sync_args(local_root))

    assert not any(p.name.startswith(".com-apple") for p in local_root.iterdir())


def test_sync_exclude_globs_bind_both_directions(
    invoke, fake_icloud, drive, local_root
):
    drive.root_node.children.append(document("remote.tmp", b"junk"))

    invoke(*sync_args(local_root, "--exclude", "*.tmp"))

    assert not (local_root / "remote.tmp").exists()
