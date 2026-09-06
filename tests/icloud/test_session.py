import pytest
from pyicloud.exceptions import (
    PyiCloudFailedLoginException,
    PyiCloudServiceNotActivatedException,
)

from isynca.errors import AuthenticationError, ConfigError, TwoFactorRequiredError
from isynca.icloud import session as mod
from tests.fakes.icloud import FakeSession

ACCOUNT = "tester@example.com"


def factory_for(fake, recorder=None):
    """Return a service factory yielding ``fake`` and recording its arguments."""

    def factory(apple_id, password=None, cookie_directory=None):
        if recorder is not None:
            recorder.append((apple_id, password, cookie_directory))
        return fake

    return factory


def test_connect_returns_ready_session():
    fake = FakeSession()
    calls = []
    api = mod.connect(ACCOUNT, service_factory=factory_for(fake, calls))
    assert api is fake
    assert calls == [(ACCOUNT, None, None)]


def test_connect_requires_an_apple_id():
    with pytest.raises(ConfigError, match="No Apple ID configured"):
        mod.connect("", service_factory=factory_for(FakeSession()))


def test_connect_creates_the_cookie_directory(tmp_path):
    cookie_dir = tmp_path / "nested" / "cookies"
    calls = []
    mod.connect(
        ACCOUNT,
        cookie_dir=cookie_dir,
        service_factory=factory_for(FakeSession(), calls),
    )
    assert cookie_dir.is_dir()
    assert calls[0][2] == str(cookie_dir)


def test_connect_passes_the_password_through():
    calls = []
    mod.connect(
        ACCOUNT, password="hunter2", service_factory=factory_for(FakeSession(), calls)
    )
    assert calls[0][1] == "hunter2"


def test_bad_credentials_raise_authentication_error():
    def factory(*args, **kwargs):
        raise PyiCloudFailedLoginException("wrong password")

    with pytest.raises(AuthenticationError, match="Login failed"):
        mod.connect(ACCOUNT, service_factory=factory)


def test_other_pyicloud_errors_raise_authentication_error():
    def factory(*args, **kwargs):
        raise PyiCloudServiceNotActivatedException("service down")

    with pytest.raises(AuthenticationError, match="Could not connect"):
        mod.connect(ACCOUNT, service_factory=factory)


def test_pending_2fa_is_fatal_when_not_interactive():
    fake = FakeSession(requires_2fa=True)
    with pytest.raises(TwoFactorRequiredError, match="isynca auth login"):
        mod.connect(ACCOUNT, service_factory=factory_for(fake))


def test_pending_2fa_without_prompt_is_fatal():
    fake = FakeSession(requires_2fa=True)
    with pytest.raises(TwoFactorRequiredError):
        mod.connect(ACCOUNT, interactive=True, service_factory=factory_for(fake))


def test_interactive_2fa_is_completed():
    fake = FakeSession(requires_2fa=True)
    mod.connect(
        ACCOUNT,
        interactive=True,
        code_prompt=lambda: " 123456 ",
        service_factory=factory_for(fake),
    )
    assert fake.validated_codes == ["123456"]
    assert not fake.requires_2fa


def test_empty_2fa_code_is_rejected():
    fake = FakeSession(requires_2fa=True)
    with pytest.raises(AuthenticationError, match="No two-factor code"):
        mod.connect(
            ACCOUNT,
            interactive=True,
            code_prompt=lambda: "   ",
            service_factory=factory_for(fake),
        )


def test_wrong_2fa_code_is_rejected():
    fake = FakeSession(requires_2fa=True, code_valid=False)
    with pytest.raises(AuthenticationError, match="was rejected"):
        mod.connect(
            ACCOUNT,
            interactive=True,
            code_prompt=lambda: "000000",
            service_factory=factory_for(fake),
        )


def test_untrusted_session_is_trusted_automatically():
    fake = FakeSession(is_trusted_session=False)
    mod.connect(ACCOUNT, service_factory=factory_for(fake))
    assert fake.trust_calls == 1
    assert fake.is_trusted_session


def test_failed_trust_is_fatal_when_not_interactive():
    fake = FakeSession(is_trusted_session=False, trust_result=False)
    with pytest.raises(TwoFactorRequiredError, match="not trusted"):
        mod.connect(ACCOUNT, service_factory=factory_for(fake))


def test_failed_trust_only_warns_when_interactive():
    fake = FakeSession(is_trusted_session=False, trust_result=False)
    api = mod.connect(
        ACCOUNT,
        interactive=True,
        code_prompt=lambda: "123456",
        service_factory=factory_for(fake),
    )
    assert api is fake


def test_status_reports_a_usable_session(monkeypatch):
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: True)
    monkeypatch.setattr(mod, "get_password_from_keyring", lambda _: "pw")
    result = mod.status(ACCOUNT, service_factory=factory_for(FakeSession()))

    assert result.apple_id == ACCOUNT
    assert result.authenticated
    assert result.trusted
    assert not result.requires_2fa
    assert result.password_stored
    assert result.usable


def test_status_reports_an_unusable_session(monkeypatch):
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: False)
    monkeypatch.setattr(mod, "get_password_from_keyring", lambda _: None)
    fake = FakeSession(requires_2fa=True, is_trusted_session=False)
    result = mod.status(ACCOUNT, service_factory=factory_for(fake))

    assert result.requires_2fa
    assert not result.usable


def test_status_survives_a_failed_connection(monkeypatch):
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: True)
    monkeypatch.setattr(mod, "get_password_from_keyring", lambda _: "pw")

    def factory(*args, **kwargs):
        raise PyiCloudFailedLoginException("nope")

    result = mod.status(ACCOUNT, service_factory=factory)
    assert not result.authenticated
    assert result.password_stored
    assert not result.usable


def test_status_survives_a_value_error(monkeypatch):
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: False)
    monkeypatch.setattr(mod, "get_password_from_keyring", lambda _: None)

    def factory(*args, **kwargs):
        raise ValueError("bad cookie jar")

    assert not mod.status(ACCOUNT, service_factory=factory).authenticated


def test_status_passes_the_cookie_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: False)
    monkeypatch.setattr(mod, "get_password_from_keyring", lambda _: None)
    calls = []
    mod.status(
        ACCOUNT, cookie_dir=tmp_path, service_factory=factory_for(FakeSession(), calls)
    )
    assert calls[0][2] == str(tmp_path)


def test_save_password_delegates_to_the_keyring(monkeypatch):
    stored = []
    monkeypatch.setattr(
        mod, "store_password_in_keyring", lambda u, p: stored.append((u, p))
    )
    mod.save_password(ACCOUNT, "pw")
    assert stored == [(ACCOUNT, "pw")]


def test_forget_password_when_present(monkeypatch):
    deleted = []
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: True)
    monkeypatch.setattr(mod, "delete_password_in_keyring", deleted.append)
    assert mod.forget_password(ACCOUNT) is True
    assert deleted == [ACCOUNT]


def test_forget_password_when_absent(monkeypatch):
    monkeypatch.setattr(mod, "password_exists_in_keyring", lambda _: False)
    assert mod.forget_password(ACCOUNT) is False


def test_clear_cookies_removes_files_only(tmp_path):
    (tmp_path / "a.cookies").write_text("x")
    (tmp_path / "b.session").write_text("y")
    (tmp_path / "subdir").mkdir()
    assert mod.clear_cookies(tmp_path) == 2
    assert (tmp_path / "subdir").is_dir()


def test_clear_cookies_on_missing_directory(tmp_path):
    assert mod.clear_cookies(tmp_path / "absent") == 0


def test_session_status_usable_requires_everything():
    status = mod.SessionStatus(
        apple_id=ACCOUNT,
        authenticated=True,
        trusted=True,
        requires_2fa=True,
        password_stored=True,
    )
    assert not status.usable


# --- remembering the signed-in account ---------------------------------------


def test_recall_without_a_stored_account(tmp_path):
    assert mod.recall_account(tmp_path) is None


def test_remember_then_recall(tmp_path):
    mod.remember_account(tmp_path, ACCOUNT)
    assert mod.recall_account(tmp_path) == ACCOUNT


def test_remember_creates_the_data_directory(tmp_path):
    target = tmp_path / "nested" / "state"
    mod.remember_account(target, ACCOUNT)
    assert mod.recall_account(target) == ACCOUNT


def test_remember_replaces_a_previous_account(tmp_path):
    mod.remember_account(tmp_path, "first@example.com")
    mod.remember_account(tmp_path, "second@example.com")
    assert mod.recall_account(tmp_path) == "second@example.com"


def test_recall_ignores_an_empty_file(tmp_path):
    (tmp_path / mod.ACCOUNT_FILE).write_text("   \n")
    assert mod.recall_account(tmp_path) is None


def test_recall_strips_whitespace(tmp_path):
    (tmp_path / mod.ACCOUNT_FILE).write_text(f"  {ACCOUNT}  \n")
    assert mod.recall_account(tmp_path) == ACCOUNT


def test_recall_survives_an_unreadable_path(tmp_path):
    """A directory where the account file should be must not raise."""
    (tmp_path / mod.ACCOUNT_FILE).mkdir()
    assert mod.recall_account(tmp_path) is None


def test_forget_account(tmp_path):
    mod.remember_account(tmp_path, ACCOUNT)
    assert mod.forget_account(tmp_path) is True
    assert mod.recall_account(tmp_path) is None


def test_forget_account_when_none_stored(tmp_path):
    assert mod.forget_account(tmp_path) is False
