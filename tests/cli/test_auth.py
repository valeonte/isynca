import pytest

from isynca.cli.app import app
from isynca.errors import AuthenticationError
from isynca.icloud import session as session_mod
from tests.fakes.icloud import FakeSession

ACCOUNT = "tester@example.com"


@pytest.fixture
def fake_connect(monkeypatch):
    """Replace the real connect() and record how it was called."""
    calls = []
    fake = FakeSession()

    def connect(**kwargs):
        calls.append(kwargs)
        return fake

    monkeypatch.setattr("isynca.cli.auth.icloud_session.connect", connect)
    return calls


def test_login_prompts_for_a_password(invoke, fake_connect, monkeypatch):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "hunter2")
    result = invoke("--apple-id", ACCOUNT, "auth", "login")

    assert result.exit_code == 0
    assert "Signed in as" in result.output
    assert fake_connect[0]["password"] == "hunter2"
    assert fake_connect[0]["interactive"] is True


def test_login_passes_a_working_2fa_prompt(invoke, fake_connect, monkeypatch):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "123456")
    invoke("--apple-id", ACCOUNT, "auth", "login")
    assert fake_connect[0]["code_prompt"]() == "123456"


def test_login_can_store_the_password(invoke, fake_connect, monkeypatch, no_keyring):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "hunter2")
    result = invoke("--apple-id", ACCOUNT, "auth", "login", "--store-password")

    assert "keyring" in result.output
    assert no_keyring[ACCOUNT] == "hunter2"


def test_login_without_storing_leaves_the_keyring_alone(
    invoke, fake_connect, monkeypatch, no_keyring
):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "hunter2")
    invoke("--apple-id", ACCOUNT, "auth", "login")
    assert no_keyring == {}


def test_login_requires_an_apple_id(invoke, fake_connect, monkeypatch):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "pw")
    result = invoke("auth", "login")
    assert result.exit_code != 0


def test_login_surfaces_authentication_failures(invoke, monkeypatch):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "pw")

    def boom(**kwargs):
        raise AuthenticationError("credentials rejected")

    monkeypatch.setattr("isynca.cli.auth.icloud_session.connect", boom)
    result = invoke("--apple-id", ACCOUNT, "auth", "login")
    assert result.exit_code != 0


def test_status_reports_a_usable_session(invoke, monkeypatch):
    status = session_mod.SessionStatus(
        apple_id=ACCOUNT,
        authenticated=True,
        trusted=True,
        requires_2fa=False,
        password_stored=True,
    )
    monkeypatch.setattr("isynca.cli.auth.icloud_session.status", lambda *a, **k: status)
    result = invoke("--apple-id", ACCOUNT, "auth", "status")

    assert result.exit_code == 0
    assert "Ready to use" in result.output
    assert "yes" in result.output


def test_status_exits_nonzero_when_unusable(invoke, monkeypatch):
    status = session_mod.SessionStatus(
        apple_id=ACCOUNT,
        authenticated=False,
        trusted=False,
        requires_2fa=True,
        password_stored=False,
    )
    monkeypatch.setattr("isynca.cli.auth.icloud_session.status", lambda *a, **k: status)
    result = invoke("--apple-id", ACCOUNT, "auth", "status")

    assert result.exit_code == 1
    assert "auth login" in result.output


def test_status_requires_an_apple_id(invoke):
    assert invoke("auth", "status").exit_code != 0


def test_logout_removes_cookies_and_password(invoke, data_dir, no_keyring):
    no_keyring[ACCOUNT] = "hunter2"
    cookie_dir = data_dir / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "session.cookies").write_text("x")

    result = invoke("--apple-id", ACCOUNT, "auth", "logout")

    assert result.exit_code == 0
    assert "Removed 1 cookie file(s)" in result.output
    assert "Removed the stored password" in result.output
    assert ACCOUNT not in no_keyring


def test_logout_when_nothing_is_stored(invoke):
    result = invoke("--apple-id", ACCOUNT, "auth", "logout")
    assert "Removed 0 cookie file(s)" in result.output
    assert "No stored password" in result.output


def test_auth_group_help(runner):
    result = runner.invoke(app, ["auth", "--help"])
    assert result.exit_code == 0
    for name in ("login", "status", "logout"):
        assert name in result.output


# --- the account is remembered across commands -------------------------------


def login_ok(invoke, monkeypatch, *extra):
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "hunter2")
    return invoke("auth", "login", "--apple-id", ACCOUNT, *extra)


def test_status_works_after_login_without_repeating_the_apple_id(
    invoke, fake_connect, monkeypatch, data_dir
):
    """The reported bug: login succeeded, then status said no Apple ID."""
    assert login_ok(invoke, monkeypatch).exit_code == 0

    status = session_mod.SessionStatus(
        apple_id=ACCOUNT,
        authenticated=True,
        trusted=True,
        requires_2fa=False,
        password_stored=False,
    )
    monkeypatch.setattr("isynca.cli.auth.icloud_session.status", lambda *a, **k: status)

    result = invoke("auth", "status")
    assert result.exit_code == 0
    assert ACCOUNT in result.output


def test_login_records_the_account(invoke, fake_connect, monkeypatch, data_dir):
    login_ok(invoke, monkeypatch)
    assert session_mod.recall_account(data_dir) == ACCOUNT


def test_a_failed_login_records_nothing(invoke, monkeypatch, data_dir):
    """A bogus account must not become the default for later commands."""
    monkeypatch.setattr("isynca.cli.auth.typer.prompt", lambda *a, **k: "pw")

    def boom(**kwargs):
        raise AuthenticationError("credentials rejected")

    monkeypatch.setattr("isynca.cli.auth.icloud_session.connect", boom)
    invoke("auth", "login", "--apple-id", ACCOUNT)
    assert session_mod.recall_account(data_dir) is None


def test_status_accepts_its_own_apple_id_option(invoke, monkeypatch):
    """It used to be accepted only before the subcommand, unlike login."""
    status = session_mod.SessionStatus(
        apple_id="other@example.com",
        authenticated=True,
        trusted=True,
        requires_2fa=False,
        password_stored=False,
    )
    monkeypatch.setattr("isynca.cli.auth.icloud_session.status", lambda *a, **k: status)
    result = invoke("auth", "status", "--apple-id", "other@example.com")
    assert result.exit_code == 0


def test_logout_accepts_its_own_apple_id_option(invoke):
    assert invoke("auth", "logout", "--apple-id", ACCOUNT).exit_code == 0


def test_logout_forgets_the_account(invoke, fake_connect, monkeypatch, data_dir):
    login_ok(invoke, monkeypatch)
    invoke("auth", "logout")

    assert session_mod.recall_account(data_dir) is None
    assert invoke("auth", "status").exit_code != 0


def test_an_explicit_apple_id_beats_the_remembered_one(
    invoke, fake_connect, monkeypatch, data_dir
):
    login_ok(invoke, monkeypatch)
    seen = []

    def record(account, **kwargs):
        seen.append(account)
        return session_mod.SessionStatus(
            apple_id=account,
            authenticated=True,
            trusted=True,
            requires_2fa=False,
            password_stored=False,
        )

    monkeypatch.setattr("isynca.cli.auth.icloud_session.status", record)
    invoke("auth", "status", "--apple-id", "override@example.com")
    assert seen == ["override@example.com"]


def test_the_remembered_account_reaches_other_commands(
    invoke, fake_connect, monkeypatch, data_dir, tmp_path
):
    """Not just auth: an upload should not need the account repeated either."""
    login_ok(invoke, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = invoke("photos", "upload", str(empty))
    assert result.exit_code == 0
