from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys

from lumberjack.schema import LogRecordRow, StoredRecord


def _make_record(**overrides: object) -> logging.LogRecord:
    kwargs: dict[str, object] = dict(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    kwargs.update(overrides)
    return logging.LogRecord(**kwargs)  # type: ignore[arg-type]


def test_from_log_record_basic_fields():
    record = _make_record()
    row = LogRecordRow.from_log_record(record)
    assert row.logger_name == "test.logger"
    assert row.level_name == "INFO"
    assert row.level_no == logging.INFO
    assert row.message == "hello world"
    assert row.lineno == 42


def test_from_log_record_reserved_fields_default_to_none():
    record = _make_record()
    row = LogRecordRow.from_log_record(record)
    assert row.task_name is None
    assert row.parent_task_id is None
    assert row.template_id is None


def test_from_log_record_captures_exception_text():
    try:
        raise ValueError("bad")
    except ValueError:
        record = _make_record(msg="failed", args=(), exc_info=sys.exc_info())
    row = LogRecordRow.from_log_record(record)
    assert row.exc_text is not None
    assert "ValueError: bad" in row.exc_text


def test_stored_record_adds_id():
    record = _make_record()
    row = LogRecordRow.from_log_record(record)
    field_values = {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}
    stored = StoredRecord(id=1, **field_values)
    assert stored.id == 1
    assert stored.message == row.message


def test_task_attribution_inside_asyncio_task():
    captured: dict[str, LogRecordRow] = {}

    async def inner() -> None:
        record = _make_record(msg="in task", args=())
        captured["row"] = LogRecordRow.from_log_record(record)

    asyncio.run(inner())
    row = captured["row"]
    assert row.task_id is not None


def test_no_task_attribution_outside_asyncio():
    record = _make_record()
    row = LogRecordRow.from_log_record(record)
    assert row.task_id is None
