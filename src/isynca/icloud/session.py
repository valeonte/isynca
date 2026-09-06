"""Authentication against iCloud, including the interactive 2FA dance.

pyicloud persists cookies in a directory of our choosing, so a trusted session
survives between runs and only the first login needs a code. Non-interactive
commands raise :class:`TwoFactorRequiredError` rather than prompting, so a
scripted upload fails with actionable advice instead of blocking on stdin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudException, PyiCloudFailedLoginException
from pyicloud.utils import (
    delete_password_in_keyring,
    get_password_from_keyring,
    password_exists_in_keyring,
    store_password_in_keyring,
)

from isynca.errors import AuthenticationError, ConfigError, TwoFactorRequiredError
from isynca.icloud.protocols import ICloudSessionLike
from isynca.logging import get_logger

LOGGER = get_logger("auth")

CodePrompt = Callable[[], str]


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """A summary of the stored session's usability."""

    apple_id: str
    authenticated: bool
    trusted: bool
    requires_2fa: bool
    password_stored: bool

    @property
    def usable(self) -> bool:
        """Return whether commands can run without further interaction."""
        return self.authenticated and self.trusted and not self.requires_2fa


def connect(
    apple_id: str,
    *,
    password: str | None = None,
    cookie_dir: Path | None = None,
    interactive: bool = False,
    code_prompt: CodePrompt | None = None,
    service_factory: Callable[..., ICloudSessionLike] = PyiCloudService,
) -> ICloudSessionLike:
    """Authenticate and return a ready-to-use session.

    Args:
        apple_id: The Apple ID to sign in as.
        password: The password; taken from the keyring when omitted.
        cookie_dir: Where pyicloud should persist session cookies.
        interactive: Whether a pending 2FA challenge may be answered by
            prompting, rather than raising.
        code_prompt: Callable returning a 2FA code; required when
            ``interactive`` is set.
        service_factory: Injection point for tests.

    Raises:
        AuthenticationError: Credentials were rejected or the session is
            unusable.
        TwoFactorRequiredError: 2FA is pending and ``interactive`` is false.
    """
    if not apple_id:
        raise ConfigError("No Apple ID configured; pass --apple-id or set apple_id")

    if cookie_dir is not None:
        cookie_dir.mkdir(parents=True, exist_ok=True)

    try:
        api = service_factory(
            apple_id,
            password,
            cookie_directory=str(cookie_dir) if cookie_dir else None,
        )
    except PyiCloudFailedLoginException as exc:
        raise AuthenticationError(f"Login failed for {apple_id}: {exc}") from exc
    except PyiCloudException as exc:
        raise AuthenticationError(f"Could not connect to iCloud: {exc}") from exc

    if api.requires_2fa:
        if not interactive or code_prompt is None:
            raise TwoFactorRequiredError(
                "This session needs two-factor authentication. "
                "Run 'isynca auth login' to complete it interactively."
            )
        _complete_two_factor(api, code_prompt)

    if not api.is_trusted_session:
        _trust(api, interactive=interactive)

    return api


def _complete_two_factor(api: ICloudSessionLike, code_prompt: CodePrompt) -> None:
    """Validate a 2FA code supplied by ``code_prompt``."""
    code = code_prompt().strip()
    if not code:
        raise AuthenticationError("No two-factor code supplied")
    if not api.validate_2fa_code(code):
        raise AuthenticationError("The two-factor code was rejected")
    LOGGER.info("Two-factor authentication completed")


def _trust(api: ICloudSessionLike, interactive: bool) -> None:
    """Request a trusted session so later runs skip the 2FA prompt."""
    try:
        trusted = bool(api.trust_session())
    except PyiCloudException as exc:  # pragma: no cover - network-only failure
        raise AuthenticationError(f"Could not establish trust: {exc}") from exc

    if trusted:
        LOGGER.info("Session trusted; future runs will not need a code")
    elif interactive:
        LOGGER.warning("Could not trust this session; a code may be needed next time")
    else:
        raise TwoFactorRequiredError(
            "The stored session is not trusted. Run 'isynca auth login'."
        )


def status(
    apple_id: str,
    *,
    cookie_dir: Path | None = None,
    service_factory: Callable[..., ICloudSessionLike] = PyiCloudService,
) -> SessionStatus:
    """Report on the stored session without prompting for anything."""
    password_stored = password_exists_in_keyring(apple_id)
    try:
        api = service_factory(
            apple_id,
            get_password_from_keyring(apple_id),
            cookie_directory=str(cookie_dir) if cookie_dir else None,
        )
    except (PyiCloudException, ValueError) as exc:
        LOGGER.debug("Session check failed: %s", exc)
        return SessionStatus(
            apple_id=apple_id,
            authenticated=False,
            trusted=False,
            requires_2fa=False,
            password_stored=password_stored,
        )

    return SessionStatus(
        apple_id=apple_id,
        authenticated=True,
        trusted=bool(api.is_trusted_session),
        requires_2fa=bool(api.requires_2fa),
        password_stored=password_stored,
    )


def save_password(apple_id: str, password: str) -> None:
    """Persist ``password`` in the system keyring."""
    store_password_in_keyring(apple_id, password)


def forget_password(apple_id: str) -> bool:
    """Remove the stored password; return whether one was present."""
    if not password_exists_in_keyring(apple_id):
        return False
    delete_password_in_keyring(apple_id)
    return True


ACCOUNT_FILE = "account"
"""Name of the file recording the last account signed in successfully.

Which account you last logged in as is session state, not configuration, so
it lives beside the cookies rather than being written into the user's
config.toml -- rewriting that file would discard their comments and layout.
"""


def remember_account(data_dir: Path, apple_id: str) -> None:
    """Record ``apple_id`` as the account later commands should default to."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ACCOUNT_FILE).write_text(f"{apple_id}\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unwritable data dir
        LOGGER.warning("Could not remember the signed-in account: %s", exc)


def recall_account(data_dir: Path) -> str | None:
    """Return the last account signed in, or ``None`` if there is none."""
    try:
        stored = (data_dir / ACCOUNT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored or None


def forget_account(data_dir: Path) -> bool:
    """Drop the remembered account; return whether one was recorded."""
    path = data_dir / ACCOUNT_FILE
    if not path.is_file():
        return False
    path.unlink()
    return True


def clear_cookies(cookie_dir: Path) -> int:
    """Delete stored session cookies; return the number of files removed."""
    if not cookie_dir.is_dir():
        return 0
    removed = 0
    for entry in cookie_dir.iterdir():
        if entry.is_file():
            entry.unlink()
            removed += 1
    return removed
