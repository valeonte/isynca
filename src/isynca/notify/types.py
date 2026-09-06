"""The shape of a desktop notification, and of anything that can send one."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol


class Urgency(IntEnum):
    """Urgency levels, numbered the way the freedesktop spec numbers them."""

    LOW = 0
    NORMAL = 1
    CRITICAL = 2


@dataclass(frozen=True, slots=True)
class Notification:
    """One message bound for the desktop's notification daemon.

    The body is plain text. Escaping it for whatever markup a particular
    daemon parses is the notifier's job, not the caller's, so that a backend
    which wants no escaping is free not to do any.
    """

    summary: str
    body: str = ""
    urgency: Urgency = Urgency.NORMAL
    icon: str = ""


class Notifier(Protocol):
    """Anything that can put a :class:`Notification` in front of the user."""

    def send(self, notification: Notification) -> None:
        """Show the notification, or do nothing if the desktop will not."""
        ...
