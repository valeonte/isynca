import logging

import pytest

from isynca.config import Config
from isynca.notify.session import MIN_SECONDS_FOR_COMPLETION, NotifySession
from isynca.notify.types import Notification, Urgency

DESKTOP_ENV = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session(notifier, clock):
    """A session with a fake notifier and a clock under test control."""
    return NotifySession(notifier=notifier, clock=clock)


@pytest.fixture
def log(session):
    """Arm the session, returning a logger wired to its collector.

    Requesting this fixture is what arms a session, the same way the root
    callback arms one by handing the handler to :func:`isynca.logging.configure`.
    """
    logger = logging.getLogger("isynca-test-session")
    logger.handlers = [session.arm(Config())]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_a_session_is_inert_until_armed(notifier):
    session = NotifySession(notifier=notifier)
    session.finished("Upload finished", "3 uploaded")
    session.close()
    assert not session.armed
    assert notifier.sent == []


def test_arming_returns_a_handler_at_the_configured_level(notifier):
    session = NotifySession(notifier=notifier)
    handler = session.arm(Config(notify_level="ERROR"))
    assert isinstance(handler, logging.Handler)
    assert handler.level == logging.ERROR
    assert session.armed


def test_notify_off_disarms_even_an_explicit_notifier(notifier):
    """--no-notify has to win over a notifier that is demonstrably available."""
    session = NotifySession(notifier=notifier)
    assert session.arm(Config(notify=False)) is None
    assert not session.armed
    session.close()
    assert notifier.sent == []


def test_without_a_desktop_session_nothing_is_armed():
    session = NotifySession()
    assert session.arm(Config(), environ={}) is None
    assert not session.armed


def test_a_desktop_session_is_detected_from_the_environment():
    session = NotifySession()
    assert session.arm(Config(), environ=DESKTOP_ENV) is not None
    assert session.armed


def test_a_quiet_short_run_says_nothing(session, notifier, log):
    session.close()
    assert notifier.sent == []


def test_a_short_run_still_reports_warnings(session, notifier, clock, log):
    log.warning("cannot read /tmp/x")
    clock.advance(1.0)
    session.finished("Upload finished", "3 uploaded")
    session.close()

    (sent,) = notifier.sent
    assert sent.summary == "isynca: 1 warning"
    assert sent.body == "cannot read /tmp/x"
    assert sent.urgency is Urgency.NORMAL


def test_a_long_clean_run_reports_finishing(session, notifier, clock, log):
    clock.advance(MIN_SECONDS_FOR_COMPLETION)
    session.finished("Upload finished", "412 uploaded")
    session.close()

    (sent,) = notifier.sent
    assert sent.summary == "isynca: Upload finished"
    assert sent.body == "412 uploaded"
    assert sent.urgency is Urgency.LOW
    assert sent.icon == "dialog-information"


def test_a_long_run_folds_its_warnings_into_one_notification(
    session, notifier, clock, log
):
    log.warning("cannot read /tmp/x")
    log.warning("no capture date in b.mov")
    clock.advance(MIN_SECONDS_FOR_COMPLETION)
    session.finished("Upload finished", "412 uploaded")
    session.close()

    (sent,) = notifier.sent
    assert sent.summary == "isynca: Upload finished"
    assert sent.body == "412 uploaded · 2 warnings"
    assert sent.urgency is Urgency.NORMAL
    assert sent.icon == "dialog-warning"


def test_counts_stand_alone_when_a_run_has_no_details(session, notifier, clock, log):
    log.error("upload refused")
    clock.advance(MIN_SECONDS_FOR_COMPLETION)
    session.finished("Upload finished")
    session.close()

    (sent,) = notifier.sent
    assert sent.body == "1 error"
    assert sent.urgency is Urgency.CRITICAL


def test_a_fatal_error_is_reported_however_short_the_run(session, notifier, log):
    session.record_fatal("Session is not trusted; run 'isynca auth login'")
    session.close()

    (sent,) = notifier.sent
    assert sent.summary == "isynca: run failed"
    assert sent.body == "Session is not trusted; run 'isynca auth login'"
    assert sent.urgency is Urgency.CRITICAL
    assert sent.icon == "dialog-error"


def test_a_fatal_error_carries_the_counts_it_died_with(session, notifier, clock, log):
    log.warning("cannot read /tmp/x")
    clock.advance(MIN_SECONDS_FOR_COMPLETION)
    session.finished("Upload finished", "3 uploaded")
    session.record_fatal("iCloud rejected the session")
    session.close()

    (sent,) = notifier.sent
    assert sent.summary == "isynca: run failed"
    assert sent.body == "iCloud rejected the session\n1 warning"


def test_closing_twice_notifies_once(session, notifier, log):
    session.record_fatal("boom")
    session.close()
    session.close()
    assert len(notifier.sent) == 1
