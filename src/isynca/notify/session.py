"""One notification per run: what to say, and whether to say anything.

A session is created inert. It starts collecting only once :meth:`arm` finds
both a configuration that permits notifications and a desktop session to send
them to, which is what keeps ``--no-notify``, cron, and CI quiet without any
of them needing a special case.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from isynca.notify.collector import NotificationCollector, clip
from isynca.notify.freedesktop import detect
from isynca.notify.types import Notification, Notifier, Urgency

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from isynca.config import Config

MIN_SECONDS_FOR_COMPLETION = 20.0
"""How long a run must take before finishing it is worth a notification.

A command you sat and watched does not need the desktop to tell you it
finished; a bulk upload you walked away from does.
"""


class NotifySession:
    """Gathers a run's notable events and reports them once, at the end."""

    def __init__(
        self,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notifier = notifier
        self._clock = clock
        self._started = clock()
        self._collector: NotificationCollector | None = None
        self._headline: str | None = None
        self._details = ""
        self._fatal: str | None = None
        self._armed = False
        self._closed = False

    @property
    def armed(self) -> bool:
        """Return whether this run will send a notification at all."""
        return self._armed

    def arm(
        self, config: Config, environ: Mapping[str, str] | None = None
    ) -> logging.Handler | None:
        """Decide whether this run notifies, returning the handler if it does.

        The handler is returned rather than installed so that
        :func:`isynca.logging.configure` stays the one place that owns the
        isynca logger's handlers.
        """
        self._armed = False
        if not config.notify:
            return None
        if self._notifier is None:
            self._notifier = detect(environ)
        if self._notifier is None:
            return None

        self._armed = True
        self._started = self._clock()
        self._collector = NotificationCollector(
            level=logging.getLevelNamesMapping()[config.notify_level]
        )
        return self._collector

    def finished(self, headline: str, details: str = "") -> None:
        """Record how a long-running command ended, for the closing report."""
        self._headline = headline
        self._details = details

    def record_fatal(self, message: str) -> None:
        """Note the error that aborted the run.

        Worth a notification however short the run was: an upload that dies
        on its first file is exactly the case you want told about.
        """
        self._fatal = clip(message)

    def close(self) -> None:
        """Send this run's single notification, if there is anything to say."""
        if not self._armed or self._closed:
            return
        self._closed = True
        notification = self._compose()
        if notification is not None and self._notifier is not None:
            self._notifier.send(notification)

    def _compose(self) -> Notification | None:
        """Build the one notification this run has earned, if any."""
        collected = self._collector.summary() if self._collector else None
        if self._fatal is not None:
            return self._failure_notification(collected)
        if self._headline is not None and self._long_enough():
            return self._completion_notification(collected)
        return collected

    def _long_enough(self) -> bool:
        """Return whether the run ran long enough to report finishing."""
        return self._clock() - self._started >= MIN_SECONDS_FOR_COMPLETION

    def _failure_notification(self, collected: Notification | None) -> Notification:
        """Build the notification for a run that stopped on a fatal error."""
        body = self._fatal or ""
        if collected is not None:
            body = f"{body}\n{collected.summary.removeprefix('isynca: ')}"
        return Notification(
            summary="isynca: run failed",
            body=body,
            urgency=Urgency.CRITICAL,
            icon="dialog-error",
        )

    def _completion_notification(self, collected: Notification | None) -> Notification:
        """Build the notification for a long run that reached its own end."""
        details = self._details
        if collected is not None:
            counts = collected.summary.removeprefix("isynca: ")
            details = f"{details} · {counts}" if details else counts
        return Notification(
            summary=f"isynca: {self._headline}",
            body=details,
            urgency=collected.urgency if collected else Urgency.LOW,
            icon=collected.icon if collected else "dialog-information",
        )
