from __future__ import annotations

import pytest

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
