from __future__ import annotations

import logging

from lumberjack.handler import LumberjackHandler
from lumberjack.schema import EXTRA_KEY
from lumberjack.store import SQLiteRecordStore


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


def test_a_record_that_cannot_be_converted_goes_to_handleError():
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
