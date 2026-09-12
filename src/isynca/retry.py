"""Retrying the failures that clear on their own.

The retry budget is split in two, because the two kinds of failure ask for
different patience. A verdict from iCloud about one file -- rate limited, a
5xx, a transfer that broke mid-stream -- is worth a few quick attempts, and
if it keeps coming back the file is better recorded as failed so the run can
move on to the next one.

A lost connection is not that. Nothing is wrong with the file, the run has
simply been unplugged from the network, and burning three attempts across
three seconds turns a thirty-second outage into thousands of files marked
failed. Those get a long, capped-delay budget instead: the run sits there
waiting for the network to come back, which is what a person watching it
would have done anyway.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from isynca.errors import ItemError
from isynca.logging import get_logger
from isynca.net import TRANSPORT_ERRORS, is_transport_error

LOGGER = get_logger("retry")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often, and how patiently, to retry a failed operation."""

    attempts: int = 3
    initial_delay: float = 1.0
    backoff: float = 2.0
    max_delay: float = 60.0
    offline_attempts: int = 15
    """Attempts granted when the connection itself failed.

    With the delays capped at a minute, the default sits out roughly ten
    minutes of lost network before giving up on a file -- long enough for a
    reconnecting router or a re-associating wifi link, short enough that a
    genuinely offline machine still finishes its run and reports.
    """

    def attempts_for(self, *, offline: bool) -> int:
        """Return how many tries an operation gets."""
        if not offline:
            return self.attempts
        return max(self.offline_attempts, self.attempts)

    def delay_for(self, attempt: int) -> float:
        """Return the delay in seconds before ``attempt`` (1-based).

        Capped, so that an outage long enough to exhaust the early doublings
        settles into steady polling rather than hour-long sleeps.
        """
        delay = self.initial_delay * (self.backoff ** (attempt - 1))
        return min(delay, self.max_delay)


def as_item_error(exc: BaseException) -> ItemError:
    """Return ``exc`` as an :class:`~isynca.errors.ItemError`.

    An error raised by an adapter already says whether it is worth retrying.
    Anything else got here raw -- a socket that broke somewhere no ``except``
    clause anticipated -- and is classified by what it is: a lost connection
    is retried, and everything else is taken at face value, since an
    unexplained error repeated is still unexplained.
    """
    if isinstance(exc, ItemError):
        return exc
    transport = is_transport_error(exc)
    return ItemError(str(exc), retryable=transport, transport=transport)


def with_retries[T](
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    label: object,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying while there is reason to believe in it.

    ``label`` names the thing being worked on in the log line, and is only
    ever formatted, never inspected.

    Raises:
        ItemError: The operation kept failing, or failed in a way that will
            not change. Raw transport failures are translated into one on the
            way out, so a caller never has to handle a socket error that
            escaped a library.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except (ItemError, *TRANSPORT_ERRORS) as exc:
            error = as_item_error(exc)
            budget = policy.attempts_for(offline=error.transport)
            if not error.retryable or attempt >= budget:
                if error is exc:
                    raise
                raise error from exc
            delay = policy.delay_for(attempt)
            LOGGER.warning(
                "Attempt %d/%d for %s failed (%s); retrying in %.1fs",
                attempt,
                budget,
                label,
                error,
                delay,
            )
            sleep(delay)


__all__ = ["RetryPolicy", "as_item_error", "with_retries"]
