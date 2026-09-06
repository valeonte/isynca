"""Desktop notifications: one summary of a run, delivered when it ends.

Warnings in isynca are per-file -- an unreadable folder, a photo with no
capture date -- and a large scan logs hundreds of them. Notifying on each
would bury the desktop, so nothing is sent while a run is in progress:
:class:`NotificationCollector` counts what was logged, and
:class:`NotifySession` turns the tally into a single notification at exit.
"""

from isynca.notify.collector import NotificationCollector
from isynca.notify.freedesktop import FreedesktopNotifier, detect
from isynca.notify.session import NotifySession
from isynca.notify.types import Notification, Notifier, Urgency

__all__ = [
    "FreedesktopNotifier",
    "Notification",
    "NotificationCollector",
    "Notifier",
    "NotifySession",
    "Urgency",
    "detect",
]
