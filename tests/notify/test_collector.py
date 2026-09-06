import logging

import pytest

from isynca.notify.collector import (
    MAX_SAMPLE_CHARS,
    MAX_SAMPLES,
    NotificationCollector,
    clip,
)
from isynca.notify.types import Urgency


@pytest.fixture
def collector():
    handler = NotificationCollector()
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


@pytest.fixture
def logger(collector):
    """A logger with the collector attached, as the CLI wires it up.

    The level check lives in ``Logger.callHandlers``, not in ``Handler.handle``,
    so notify_level can only be exercised through a logger.
    """
    log = logging.getLogger("isynca-test-collector")
    log.handlers = [collector]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log


def record(level=logging.WARNING, msg="something", args=()):
    return logging.LogRecord("isynca", level, __file__, 1, msg, args, None)


def test_nothing_collected_summarises_to_nothing(collector):
    assert collector.summary() is None
    assert collector.total == 0
    assert collector.worst == 0


def test_counts_and_headline(collector):
    for _ in range(3):
        collector.handle(record(logging.WARNING))
    collector.handle(record(logging.ERROR))
    assert collector.total == 4
    assert collector.headline() == "1 error, 3 warnings"


def test_headline_is_singular_for_one(collector):
    collector.handle(record(logging.WARNING))
    assert collector.headline() == "1 warning"


def test_critical_counts_as_an_error(collector):
    collector.handle(record(logging.CRITICAL))
    assert collector.headline() == "1 error"
    assert collector.urgency() is Urgency.CRITICAL


def test_records_below_the_level_are_ignored(collector, logger):
    logger.info("chatter")
    logger.warning("trouble")
    assert collector.total == 1


def test_a_raised_level_ignores_warnings(logger):
    collector = NotificationCollector(level=logging.ERROR)
    logger.handlers = [collector]
    logger.warning("trouble")
    logger.error("worse")
    assert collector.total == 1


def test_body_samples_the_first_few_and_counts_the_rest(collector):
    for index in range(MAX_SAMPLES + 5):
        collector.handle(record(logging.WARNING, f"warning {index}"))
    body = collector.body()
    assert body.splitlines()[:MAX_SAMPLES] == [
        f"warning {index}" for index in range(MAX_SAMPLES)
    ]
    assert body.endswith("… and 5 more")


def test_an_error_displaces_the_warnings_sampled_before_it(collector):
    """The body has room for three lines; the worst thing should get them."""
    collector.handle(record(logging.WARNING, "no capture date"))
    collector.handle(record(logging.ERROR, "upload refused"))
    body = collector.body()
    assert "upload refused" in body
    assert "no capture date" not in body
    assert body.endswith("… and 1 more")


def test_body_has_no_more_line_when_everything_is_shown(collector):
    collector.handle(record(logging.WARNING, "only one"))
    assert collector.body() == "only one"


def test_clip_flattens_and_truncates():
    assert clip("  two   words\nhere ") == "two words here"
    clipped = clip("x" * (MAX_SAMPLE_CHARS + 50))
    assert len(clipped) == MAX_SAMPLE_CHARS
    assert clipped.endswith("…")


def test_summary_describes_warnings(collector):
    collector.handle(record(logging.WARNING, "cannot read /tmp/x"))
    summary = collector.summary()
    assert summary is not None
    assert summary.summary == "isynca: 1 warning"
    assert summary.body == "cannot read /tmp/x"
    assert summary.urgency is Urgency.NORMAL
    assert summary.icon == "dialog-warning"


def test_summary_escalates_for_errors(collector):
    collector.handle(record(logging.ERROR, "upload refused"))
    summary = collector.summary()
    assert summary is not None
    assert summary.urgency is Urgency.CRITICAL
    assert summary.icon == "dialog-error"


def test_a_broken_record_is_counted_but_does_not_raise(collector, monkeypatch):
    """A logging handler that raises would take the whole run down with it."""
    monkeypatch.setattr(logging, "raiseExceptions", False)
    collector.handle(record(logging.WARNING, "%d files", ("not a number",)))
    assert collector.total == 1
    assert collector.body() == "… and 1 more"
