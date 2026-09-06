"""Recording what a run logged, so it can be summarised once at the end.

This handler does no I/O at all: it counts records and keeps a few example
lines, which is all a 10,000-file scan can afford to spend on notifications.
Turning the tally into something the desktop shows is
:class:`isynca.notify.session.NotifySession`'s job.
"""

from __future__ import annotations

import logging
from collections import Counter

from isynca.notify.types import Notification, Urgency

MAX_SAMPLES = 3
"""How many example lines a notification body carries."""

MAX_SAMPLE_CHARS = 140
"""Longest example line kept; paths run long and daemons truncate anyway."""


def _label(levelno: int) -> str:
    """Return the word this level is counted under.

    Critical counts as an error rather than earning a noun of its own:
    "2 criticals" reads badly, and the urgency flag already tells the daemon
    to keep the notification on screen.
    """
    return "error" if levelno >= logging.ERROR else "warning"


def clip(text: str) -> str:
    """Flatten one log line into something a notification body can show."""
    line = " ".join(text.split())
    if len(line) <= MAX_SAMPLE_CHARS:
        return line
    return line[: MAX_SAMPLE_CHARS - 1] + "…"


class NotificationCollector(logging.Handler):
    """Counts warnings and errors, and summarises them on request."""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self.counts: Counter[int] = Counter()
        self._samples: list[str] = []
        self._sample_level = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Count the record, keeping the first few of the worst level seen.

        Once an error arrives, the warnings sampled before it are dropped:
        the body has room for three lines, and they are better spent on the
        worst thing that happened than on the first thing.
        """
        self.counts[record.levelno] += 1
        try:
            if record.levelno > self._sample_level:
                self._sample_level = record.levelno
                self._samples = []
            if (
                record.levelno == self._sample_level
                and len(self._samples) < MAX_SAMPLES
            ):
                self._samples.append(clip(self.format(record)))
        except Exception:
            # A handler that raises would take the run down with it, and a
            # notification is never worth that.
            self.handleError(record)

    @property
    def total(self) -> int:
        """Return how many records were collected."""
        return sum(self.counts.values())

    @property
    def worst(self) -> int:
        """Return the highest level seen, or 0 when nothing was collected."""
        return max(self.counts, default=0)

    def headline(self) -> str:
        """Return a phrase like ``3 errors, 12 warnings``."""
        tally: Counter[str] = Counter()
        for levelno, count in self.counts.items():
            tally[_label(levelno)] += count
        return ", ".join(
            f"{tally[name]} {name}" + ("" if tally[name] == 1 else "s")
            for name in ("error", "warning")
            if tally[name]
        )

    def body(self) -> str:
        """Return the example lines, with a count of everything not shown."""
        remaining = self.total - len(self._samples)
        lines = list(self._samples)
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        return "\n".join(lines)

    def urgency(self) -> Urgency:
        """Return the urgency the collected records deserve."""
        return Urgency.CRITICAL if self.worst >= logging.ERROR else Urgency.NORMAL

    def icon(self) -> str:
        """Return the stock icon name matching the worst level seen."""
        return "dialog-error" if self.worst >= logging.ERROR else "dialog-warning"

    def summary(self) -> Notification | None:
        """Return a notification for what was collected, or ``None``."""
        if not self.counts:
            return None
        return Notification(
            summary=f"isynca: {self.headline()}",
            body=self.body(),
            urgency=self.urgency(),
            icon=self.icon(),
        )
