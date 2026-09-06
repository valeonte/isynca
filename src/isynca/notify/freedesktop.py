"""Sending notifications over D-Bus, the way every Linux desktop expects.

Plasma, GNOME, Cinnamon and XFCE all implement the same freedesktop
``org.freedesktop.Notifications`` interface, so there is nothing
desktop-specific to detect here: if the session bus answers, the notification
appears. jeepney speaks that bus in pure Python, with no compiled extension
and no daemon of its own.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
from typing import TYPE_CHECKING, Any

from isynca.logging import get_logger
from isynca.notify.types import Notification, Notifier

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# Everything in this module logs at DEBUG and never above. A failed
# notification is cosmetic, and a warning about one would be collected by the
# very handler whose report is being delivered.
LOGGER = get_logger("notify")

APP_NAME = "isynca"
BUS_ADDRESS_ENV = "DBUS_SESSION_BUS_ADDRESS"
BUS_PATH = "/org/freedesktop/Notifications"
BUS_NAME = "org.freedesktop.Notifications"
NOTIFY_SIGNATURE = "susssasa{sv}i"
DBUS_TIMEOUT = 2.0
"""Seconds to wait for the daemon. Long enough to answer, short enough that a
wedged daemon does not hold up a finished command."""


def escape_markup(text: str) -> str:
    """Escape the markup subset notification daemons parse in a body.

    Plasma renders the body as markup, so a filename containing ``&`` or
    ``<`` would otherwise come out mangled or disappear entirely.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _open_session_bus() -> Any:  # noqa: ANN401 - jeepney's connection type
    """Connect to the session bus."""
    # Imported here, not at module scope: jeepney is only needed by a run that
    # actually has something to say, and an install without it should degrade
    # to silence rather than fail to start.
    from jeepney.io.blocking import open_dbus_connection  # noqa: PLC0415

    return open_dbus_connection(bus="SESSION")


def _build_message(notification: Notification) -> Any:  # noqa: ANN401
    """Build the ``Notify`` method call for one notification."""
    from jeepney import DBusAddress, new_method_call  # noqa: PLC0415 - see above

    address = DBusAddress(BUS_PATH, bus_name=BUS_NAME, interface=BUS_NAME)
    return new_method_call(
        address,
        "Notify",
        NOTIFY_SIGNATURE,
        (
            APP_NAME,
            0,  # replaces_id: each run's summary stands on its own
            notification.icon,
            notification.summary,
            escape_markup(notification.body),
            [],  # actions: nothing here is clickable
            {
                "urgency": ("y", int(notification.urgency)),
                # What Plasma groups and labels the popup by.
                "desktop-entry": ("s", APP_NAME),
            },
            -1,  # expire_timeout: whatever the daemon considers normal
        ),
    )


class FreedesktopNotifier:
    """Sends notifications to the session bus's notification daemon."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or _open_session_bus

    def send(self, notification: Notification) -> None:
        """Deliver the notification, swallowing anything that goes wrong.

        A desktop that will not take the message is a cosmetic problem, never
        a reason to fail a run that has otherwise finished, so every failure
        is logged at DEBUG and dropped.
        """
        try:
            connection = self._connect()
        except Exception as exc:
            LOGGER.debug("No notification bus: %s", exc)
            return
        try:
            connection.send_and_get_reply(
                _build_message(notification), timeout=DBUS_TIMEOUT
            )
        except Exception as exc:
            LOGGER.debug("Could not send notification: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                connection.close()


def detect(environ: Mapping[str, str] | None = None) -> Notifier | None:
    """Return a notifier when this process is talking to a desktop session.

    The session bus address is the only signal worth trusting: a cron job, an
    ssh session, or CI has no bus, and a notification there is either
    impossible or invisible. jeepney being absent is treated the same way, so
    an install without it degrades to silence rather than an ImportError.
    """
    source = os.environ if environ is None else environ
    if not source.get(BUS_ADDRESS_ENV):
        LOGGER.debug("No %s set; desktop notifications are off", BUS_ADDRESS_ENV)
        return None
    if importlib.util.find_spec("jeepney") is None:
        LOGGER.debug("jeepney is not installed; desktop notifications are off")
        return None
    return FreedesktopNotifier()
