from __future__ import annotations

import logging

import pytest

from lumberjack.handler import LumberjackHandler
from lumberjack.schema import EXTRA_KEY
from lumberjack.store import SQLiteRecordStore

#: Tier 2 — a component contract, driven through a public component API.
#: See tests/README.md; `test_tier2_rules.py` checks what the mark claims.
pytestmark = pytest.mark.tier2


def test_emit_appends_to_buffer(attached_logger):
    handler = LumberjackHandler()
    with attached_logger(handler) as logger:
        logger.info("hello")
    rows = handler.drain()
    assert len(rows) == 1
    assert rows[0].message == "hello"


def test_drain_empties_buffer_in_order(attached_logger):
    handler = LumberjackHandler()
    with attached_logger(handler) as logger:
        logger.info("first")
        logger.info("second")
    drained = handler.drain()
    assert [r.message for r in drained] == ["first", "second"]
    assert handler.drain() == []


def test_buffer_is_bounded(attached_logger):
    handler = LumberjackHandler(buffer_size=2)
    with attached_logger(handler) as logger:
        logger.info("a")
        logger.info("b")
        logger.info("c")
    rows = handler.drain()
    assert [r.message for r in rows] == ["b", "c"]


def test_overflow_is_counted_not_silent(attached_logger):
    """A full buffer evicts the oldest row; the store loses it, so say so."""
    handler = LumberjackHandler(buffer_size=2)
    assert handler.dropped == 0
    with attached_logger(handler) as logger:
        for msg in ("a", "b"):
            logger.info(msg)
        assert handler.dropped == 0, "a buffer that is merely full has lost nothing"
        for msg in ("c", "d", "e"):
            logger.info(msg)
    assert handler.dropped == 3


def test_dropped_count_survives_a_drain(attached_logger):
    """Cumulative for the session: draining doesn't forgive earlier losses."""
    handler = LumberjackHandler(buffer_size=1)
    with attached_logger(handler) as logger:
        for msg in ("a", "b", "c"):
            logger.info(msg)
        assert handler.dropped == 2
        handler.drain()
        logger.info("d")
    assert handler.dropped == 2


def test_on_record_callback_invoked_per_record(attached_logger):
    seen: list[str] = []
    handler = LumberjackHandler(on_record=lambda row: seen.append(row.message))
    with attached_logger(handler) as logger:
        logger.info("hi")
    assert seen == ["hi"]


def test_a_raising_callback_still_buffers_the_record(attached_logger):
    """Only the live view is lost. The record was buffered before the callback
    ran, so the store — the half that must never lose anything — still gets
    it. `Logger.callHandlers` has no catch of its own, so an unguarded
    callback would otherwise surface out of an ordinary `log.info()` in code
    that has never heard of lumberjack; a dead stderr consumer is the
    realistic way in, which is why the callback is made to raise one here."""
    handler = LumberjackHandler(on_record=_raise_broken_pipe)
    handler.handleError = lambda record: None
    with attached_logger(handler) as logger:
        logger.info("survived")
    assert [row.message for row in handler.drain()] == ["survived"]


def _raise_broken_pipe(row: object) -> None:
    raise BrokenPipeError("the stderr consumer died")


def test_a_task_event_travels_extra_through_the_handler_to_the_store(
    attached_logger, make_task_event
):
    """The whole seam, end to end on real objects: a `logging` call carrying
    `extra={EXTRA_KEY: TaskEvent(...)}` comes back out of the store with its
    progress columns intact. This is the route the tracking API takes, and the
    reason it needs no side-channel write of its own — the handler stays the
    only writer."""
    handler = LumberjackHandler(level=logging.DEBUG)
    store = SQLiteRecordStore(":memory:")
    try:
        with attached_logger(handler) as logger:
            logger.info(
                "task progress: reindex 40/100",
                extra={EXTRA_KEY: make_task_event()},
            )
        store.append(handler.drain())
        (row,) = store.recent()
    finally:
        store.close()
    assert row.task_label == "reindex"
    assert row.task_event == "update"
    assert (row.task_id, row.parent_task_id) == (7, 3)
    assert (row.progress_current, row.progress_total) == (40, 100)
    assert row.message == "task progress: reindex 40/100"


def test_a_record_that_cannot_be_converted_goes_to_handleError():  # noqa: N802 - names stdlib's handleError; the casing is stdlib's
    """stdlib's contract for a bad record, and the one path `emit()` swallows.

    A broken record must not take down the `log.info()` that produced it, and
    must not silently vanish either — `handleError` is where stdlib says it
    goes. Previously uncovered, which meant the `except` was untested.
    """
    handler = LumberjackHandler()
    handled: list[logging.LogRecord] = []
    handler.handleError = handled.append

    # Two placeholders, one argument: `getMessage()` raises TypeError inside
    # `from_log_record`. An *empty* args tuple would not — stdlib skips the
    # `%` entirely when there is nothing to interpolate.
    record = logging.LogRecord("n", logging.INFO, "p.py", 1, "%d %d", (1,), None)

    handler.emit(record)

    assert handled == [record]
    assert handler.drain() == [], "a record that failed conversion must not be stored"


# --- restore(): the retry path a failed store write needs -------------------


def test_restore_puts_a_failed_batch_back_in_front(attached_logger):
    """`drain()` is destructive, so a raising `append()` used to lose the
    batch outright — a silent loss, which is the one thing Principle 6 does
    not permit. The rows go back at the *front*: they are older than anything
    that arrived while the write was failing."""
    handler = LumberjackHandler()
    with attached_logger(handler) as logger:
        logger.info("first")
        logger.info("second")
        failed = handler.drain()
        logger.info("arrived during the failure")

    handler.restore(failed)

    assert [row.message for row in handler.drain()] == [
        "first",
        "second",
        "arrived during the failure",
    ]


def test_restore_is_bounded_and_counts_what_will_not_fit(attached_logger):
    """A permanently failing store must not grow the retry queue without
    limit. What no longer fits is counted as dropped, so the exit report
    covers it for free rather than needing a second message."""
    handler = LumberjackHandler(buffer_size=3)
    with attached_logger(handler) as logger:
        for i in range(3):
            logger.info("old %d", i)
        failed = handler.drain()
        logger.info("new")

    assert handler.dropped == 0
    handler.restore(failed)

    # Two of the three retried rows fit alongside the one that arrived since.
    assert handler.dropped == 1
    assert [row.message for row in handler.drain()] == ["old 1", "old 2", "new"]


def test_restore_drops_the_oldest_rather_than_the_newest(attached_logger):
    """A full `deque` discards from the far end, so letting `extendleft`
    spill would evict the records the buffer already holds in order to make
    room for older ones being retried. Dropping the oldest is the rule the
    buffer already applies when it overflows."""
    handler = LumberjackHandler(buffer_size=2)
    with attached_logger(handler) as logger:
        logger.info("oldest")
        logger.info("older")
        failed = handler.drain()
        logger.info("newest")

    handler.restore(failed)
    assert [row.message for row in handler.drain()] == ["older", "newest"]


def test_restoring_more_than_the_buffer_holds_keeps_the_newest(attached_logger):
    handler = LumberjackHandler(buffer_size=2)
    with attached_logger(handler) as logger:
        for i in range(5):
            logger.info("row %d", i)
    # Three were evicted on the way in, so `drain()` returns the last two.
    failed = handler.drain()
    handler.restore(failed)
    assert [row.message for row in handler.drain()] == ["row 3", "row 4"]
