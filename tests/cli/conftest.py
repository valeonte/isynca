from __future__ import annotations

import pytest
from typer.testing import CliRunner

from isynca.cli.app import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    """Keep CLI tests away from the real system keyring.

    Probing keyring backends can reach out to a session bus or network
    service, which pytest-socket rightly blocks; stubbing it keeps these
    tests hermetic and fast.
    """
    store: dict[str, str] = {}
    monkeypatch.setattr(
        "isynca.icloud.session.password_exists_in_keyring", lambda u: u in store
    )
    monkeypatch.setattr("isynca.icloud.session.get_password_from_keyring", store.get)
    monkeypatch.setattr(
        "isynca.icloud.session.store_password_in_keyring", store.__setitem__
    )
    monkeypatch.setattr(
        "isynca.icloud.session.delete_password_in_keyring", lambda u: store.pop(u, None)
    )
    return store


@pytest.fixture
def invoke(runner, data_dir, monkeypatch):
    """Invoke the CLI with an isolated data dir and no ambient config."""
    monkeypatch.delenv("ISYNCA_APPLE_ID", raising=False)
    # Rich wraps to the terminal width; pin it so assertions on output are
    # not at the mercy of the environment running the suite.
    monkeypatch.setenv("COLUMNS", "200")

    def run(*args: str, config: str | None = None):
        base = ["--data-dir", str(data_dir)]
        base += ["--config", config or str(data_dir / "absent.toml")]
        return runner.invoke(app, [*base, *args])

    return run
