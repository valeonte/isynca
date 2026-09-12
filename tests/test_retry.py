import errno

import pytest

from isynca.errors import FatalError, ItemError
from isynca.retry import RetryPolicy, as_item_error, with_retries


def run(operation, policy=None, delays=None):
    return with_retries(
        operation,
        policy=policy or RetryPolicy(),
        label="clip.mp4",
        sleep=(delays if delays is not None else []).append,
    )


def failing(*errors, result="done"):
    """Return a callable raising each error in turn, then returning ``result``."""
    queue = list(errors)

    def operation():
        if queue:
            raise queue.pop(0)
        return result

    return operation


def test_a_working_operation_is_run_once():
    calls = []
    assert run(lambda: calls.append(1) or "done") == "done"
    assert calls == [1]


def test_a_retryable_failure_is_retried_then_succeeds():
    delays = []
    assert run(failing(ItemError("flaky")), delays=delays) == "done"
    assert delays == [1.0]


def test_a_settled_failure_is_raised_at_once():
    delays = []
    with pytest.raises(ItemError, match="refused"):
        run(failing(ItemError("refused", retryable=False)), delays=delays)
    assert delays == []


def test_attempts_are_bounded():
    delays = []
    with pytest.raises(ItemError):
        run(failing(*[ItemError("down") for _ in range(5)]), delays=delays)
    assert delays == [1.0, 2.0]


def test_a_fatal_error_is_not_retried():
    with pytest.raises(FatalError):
        run(failing(FatalError("session revoked")))


def test_a_dropped_connection_is_waited_out_rather_than_failed():
    """The blip that started this: a raw socket error, mid-run, mid-upload.

    It escapes as an ``OSError`` from deep inside the HTTP stack, so nothing
    below classified it, and it must not cost the file its three attempts.
    """
    delays = []
    blips = [OSError(errno.EHOSTUNREACH, "No route to host") for _ in range(6)]
    assert run(failing(*blips), delays=delays) == "done"
    assert len(delays) == 6
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]


def test_an_outage_outlasting_the_offline_budget_ends_as_an_item_error():
    policy = RetryPolicy(offline_attempts=3)
    delays = []
    blips = [OSError(errno.ENETUNREACH, "Network is unreachable") for _ in range(3)]
    with pytest.raises(ItemError, match="Network is unreachable") as raised:
        run(failing(*blips), policy=policy, delays=delays)

    assert raised.value.transport
    assert delays == [1.0, 2.0]


def test_a_local_failure_is_not_waited_out():
    """A file that will not read reads no better in a minute."""
    delays = []
    with pytest.raises(ItemError, match="disk vanished"):
        run(failing(OSError("disk vanished")), delays=delays)
    assert delays == []


def test_delays_are_capped():
    policy = RetryPolicy(initial_delay=1.0, backoff=10.0, max_delay=30.0)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [1.0, 10.0, 30.0, 30.0]


def test_the_offline_budget_is_never_smaller_than_the_ordinary_one():
    policy = RetryPolicy(attempts=20, offline_attempts=5)
    assert policy.attempts_for(offline=True) == 20
    assert policy.attempts_for(offline=False) == 20


def test_an_item_error_is_passed_through_unchanged():
    error = ItemError("already classified", retryable=False)
    assert as_item_error(error) is error
