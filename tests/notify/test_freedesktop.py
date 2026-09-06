import importlib.util

import jeepney.io.blocking
import pytest
from jeepney import HeaderFields

from isynca.notify.freedesktop import (
    BUS_ADDRESS_ENV,
    FreedesktopNotifier,
    _open_session_bus,
    detect,
    escape_markup,
)
from isynca.notify.types import Notification, Urgency

DESKTOP_ENV = {BUS_ADDRESS_ENV: "unix:path=/run/user/1000/bus"}


class FakeConnection:
    """Stands in for a jeepney session bus connection."""

    def __init__(self, fail: bool = False) -> None:
        self.sent = []
        self.closed = False
        self.fail = fail

    def send_and_get_reply(self, message, timeout=None):
        self.sent.append((message, timeout))
        if self.fail:
            raise OSError("bus went away")
        return message

    def close(self):
        self.closed = True


def test_escape_markup_protects_a_body():
    """Plasma parses the body as markup; an unescaped path comes out mangled."""
    assert escape_markup("a & b <c> d") == "a &amp; b &lt;c&gt; d"


def test_send_builds_a_spec_compliant_notify_call():
    connection = FakeConnection()
    FreedesktopNotifier(connect=lambda: connection).send(
        Notification(
            "isynca: 1 error", "R&D/clip.mp4", Urgency.CRITICAL, "dialog-error"
        )
    )

    message, timeout = connection.sent[0]
    assert timeout is not None
    assert message.header.fields[HeaderFields.member] == "Notify"
    app_name, replaces_id, icon, summary, body, actions, hints, expires = message.body
    assert app_name == "isynca"
    assert replaces_id == 0
    assert icon == "dialog-error"
    assert summary == "isynca: 1 error"
    assert body == "R&amp;D/clip.mp4"
    assert actions == []
    assert hints["urgency"] == ("y", 2)
    assert hints["desktop-entry"] == ("s", "isynca")
    assert expires == -1
    assert connection.closed


def test_a_bus_that_will_not_connect_is_not_an_error():
    def refuse():
        raise ConnectionRefusedError("no daemon")

    FreedesktopNotifier(connect=refuse).send(Notification("hi"))


def test_a_failed_send_is_swallowed_and_still_closes():
    connection = FakeConnection(fail=True)
    FreedesktopNotifier(connect=lambda: connection).send(Notification("hi"))
    assert connection.closed


def test_a_connection_that_will_not_close_is_ignored():
    class Stubborn(FakeConnection):
        def close(self):
            raise OSError("still busy")

    FreedesktopNotifier(connect=Stubborn).send(Notification("hi"))


def test_detect_without_a_session_bus_returns_nothing():
    """Cron, ssh, and CI have no bus, and must not pay for finding out."""
    assert detect({}) is None


def test_detect_with_a_session_bus_returns_a_notifier():
    assert isinstance(detect(DESKTOP_ENV), FreedesktopNotifier)


def test_detect_without_jeepney_returns_nothing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert detect(DESKTOP_ENV) is None


def test_detect_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.setenv(BUS_ADDRESS_ENV, "unix:path=/run/user/1000/bus")
    assert isinstance(detect(), FreedesktopNotifier)


@pytest.mark.parametrize("urgency", list(Urgency))
def test_every_urgency_survives_the_wire_format(urgency):
    connection = FakeConnection()
    FreedesktopNotifier(connect=lambda: connection).send(
        Notification("s", urgency=urgency)
    )
    assert connection.sent[0][0].body[6]["urgency"] == ("y", int(urgency))


def test_the_default_connection_asks_jeepney_for_the_session_bus(monkeypatch):
    """The one line no test can exercise for real: pytest-socket blocks it."""
    asked = []

    def fake_open(bus):
        asked.append(bus)

    monkeypatch.setattr(jeepney.io.blocking, "open_dbus_connection", fake_open)
    _open_session_bus()
    assert asked == ["SESSION"]
