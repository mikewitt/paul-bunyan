from __future__ import annotations

import logging

from lumberjack.handler import DEFAULT_BUFFER_SIZE, LumberjackHandler
from lumberjack.schema import EXTRA_KEY, TaskEvent
from lumberjack.store import SQLiteRecordStore


def _emit(handler: LumberjackHandler, message: str) -> None:
    logger = logging.getLogger("handler-test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info(message)
    finally:
        logger.removeHandler(handler)


def test_emit_appends_to_buffer():
    handler = LumberjackHandler()
    _emit(handler, "hello")
    rows = handler.drain()
    assert len(rows) == 1
    assert rows[0].message == "hello"


def test_drain_empties_buffer_in_order():
    handler = LumberjackHandler()
    _emit(handler, "first")
    _emit(handler, "second")
    drained = handler.drain()
    assert [r.message for r in drained] == ["first", "second"]
    assert handler.drain() == []


def test_buffer_is_bounded():
    handler = LumberjackHandler(buffer_size=2)
    _emit(handler, "a")
    _emit(handler, "b")
    _emit(handler, "c")
    rows = handler.drain()
    assert [r.message for r in rows] == ["b", "c"]


def test_overflow_is_counted_not_silent():
    """A full buffer evicts the oldest row; the store loses it, so say so."""
    handler = LumberjackHandler(buffer_size=2)
    assert handler.dropped == 0
    for msg in ("a", "b"):
        _emit(handler, msg)
    assert handler.dropped == 0, "a buffer that is merely full has lost nothing"
    for msg in ("c", "d", "e"):
        _emit(handler, msg)
    assert handler.dropped == 3


def test_dropped_count_survives_a_drain():
    """Cumulative for the session: draining doesn't forgive earlier losses."""
    handler = LumberjackHandler(buffer_size=1)
    for msg in ("a", "b", "c"):
        _emit(handler, msg)
    assert handler.dropped == 2
    handler.drain()
    _emit(handler, "d")
    assert handler.dropped == 2


def test_on_record_callback_invoked_per_record():
    seen: list[str] = []
    handler = LumberjackHandler(on_record=lambda row: seen.append(row.message))
    _emit(handler, "hi")
    assert seen == ["hi"]


def test_a_raising_callback_does_not_escape_into_the_logging_call():
    """`Logger.callHandlers` has no catch of its own, so an unguarded callback
    surfaces out of an ordinary `log.info()` in code that has never heard of
    lumberjack. A dead stderr consumer is the realistic way in."""
    handler = LumberjackHandler(on_record=_raise_broken_pipe, level=logging.DEBUG)
    handler.handleError = lambda record: None
    logger = logging.getLogger("raising-callback")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info("an ordinary log line")
    finally:
        logger.removeHandler(handler)


def test_a_raising_callback_still_buffers_the_record():
    """Only the live view is lost. The record was buffered before the callback
    ran, so the store — the half that must never lose anything — still gets
    it."""
    handler = LumberjackHandler(on_record=_raise_broken_pipe)
    handler.handleError = lambda record: None
    _emit(handler, "survived")
    assert [row.message for row in handler.drain()] == ["survived"]


def _raise_broken_pipe(row: object) -> None:
    raise BrokenPipeError("the stderr consumer died")


def test_default_buffer_size_is_positive():
    assert DEFAULT_BUFFER_SIZE > 0


def test_a_task_event_travels_extra_through_the_handler_to_the_store():
    """The whole seam, end to end on real objects: a `logging` call carrying
    `extra={EXTRA_KEY: TaskEvent(...)}` comes back out of the store with its
    progress columns intact. This is the route the tracking API takes, and the
    reason it needs no side-channel write of its own — the handler stays the
    only writer."""
    handler = LumberjackHandler(level=logging.DEBUG)
    store = SQLiteRecordStore(":memory:")
    logger = logging.getLogger("task-event-roundtrip")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info(
            "task progress: reindex 40/100",
            extra={
                EXTRA_KEY: TaskEvent(
                    label="reindex",
                    kind="update",
                    task_id=7,
                    parent_task_id=3,
                    current=40,
                    total=100,
                )
            },
        )
        store.append(handler.drain())
        (row,) = store.recent()
    finally:
        logger.removeHandler(handler)
        store.close()
    assert row.task_label == "reindex"
    assert row.task_event == "update"
    assert (row.task_id, row.parent_task_id) == (7, 3)
    assert (row.progress_current, row.progress_total) == (40, 100)
    assert row.message == "task progress: reindex 40/100"
