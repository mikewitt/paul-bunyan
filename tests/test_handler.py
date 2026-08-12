from __future__ import annotations

import logging

from lumberjack.handler import DEFAULT_BUFFER_SIZE, LumberjackHandler


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
    rows = handler.peek()
    assert len(rows) == 1
    assert rows[0].message == "hello"


def test_drain_empties_buffer_in_order():
    handler = LumberjackHandler()
    _emit(handler, "first")
    _emit(handler, "second")
    drained = handler.drain()
    assert [r.message for r in drained] == ["first", "second"]
    assert handler.peek() == []


def test_buffer_is_bounded():
    handler = LumberjackHandler(buffer_size=2)
    _emit(handler, "a")
    _emit(handler, "b")
    _emit(handler, "c")
    rows = handler.peek()
    assert [r.message for r in rows] == ["b", "c"]


def test_on_record_callback_invoked_per_record():
    seen: list[str] = []
    handler = LumberjackHandler(on_record=lambda row: seen.append(row.message))
    _emit(handler, "hi")
    assert seen == ["hi"]


def test_peek_with_n_returns_last_n():
    handler = LumberjackHandler()
    for msg in ("a", "b", "c"):
        _emit(handler, msg)
    assert [r.message for r in handler.peek(2)] == ["b", "c"]


def test_default_buffer_size_is_positive():
    assert DEFAULT_BUFFER_SIZE > 0
