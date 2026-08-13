from __future__ import annotations

import asyncio
import dataclasses
import gc
import logging
import sys

from lumberjack.schema import EXTRA_KEY, LogRecordRow, StoredRecord, TaskEvent


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
    assert row.asyncio_task_name is None
    assert row.task_id is None
    assert row.parent_task_id is None
    assert row.template_id is None


# --- the `extra=` seam the tracking API writes through ----------------------


def _record_carrying(payload: object) -> logging.LogRecord:
    """A record with `payload` under the extra key.

    `setattr` after construction is precisely what `logging` does with
    `extra=` — `Logger.makeRecord` copies the mapping onto `record.__dict__`.
    """
    record = _make_record()
    setattr(record, EXTRA_KEY, payload)
    return record


def test_a_task_event_populates_the_progress_columns():
    event = TaskEvent(
        label="reindex",
        kind="update",
        task_id=7,
        parent_task_id=3,
        current=40,
        total=100,
    )
    row = LogRecordRow.from_log_record(_record_carrying(event))
    assert (row.task_label, row.task_event) == ("reindex", "update")
    assert (row.task_id, row.parent_task_id) == (7, 3)
    assert (row.progress_current, row.progress_total) == (40, 100)


def test_a_task_event_without_progress_leaves_those_columns_null():
    """An indeterminate task is the common case: `task()` with no total."""
    event = TaskEvent(label="migrate", kind="start", task_id=1)
    row = LogRecordRow.from_log_record(_record_carrying(event))
    assert (row.task_label, row.task_event) == ("migrate", "start")
    assert (row.progress_current, row.progress_total) == (None, None)
    assert row.parent_task_id is None


def test_a_foreign_attribute_under_our_key_is_ignored():
    """Somebody else's `extra={"lumberjack": ...}` must not crash `emit()` for
    every record in the process — it degrades to a record with no task data."""
    row = LogRecordRow.from_log_record(_record_carrying({"label": "not ours"}))
    assert row.task_label is None
    assert row.task_event is None
    assert row.task_id is None


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
    assert row.asyncio_task_id is not None


def test_no_task_attribution_outside_asyncio():
    record = _make_record()
    row = LogRecordRow.from_log_record(record)
    assert row.asyncio_task_id is None


def test_no_task_attribution_in_a_bare_loop_callback():
    """A running loop is not a running task. `current_task()` returns None
    rather than raising inside a `call_soon` callback, so that is a third
    case, distinct from both 'in a task' and 'no loop at all'."""
    ids: list[int | None] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def callback() -> None:
            ids.append(LogRecordRow.from_log_record(_make_record()).asyncio_task_id)
            done.set_result(None)

        loop.call_soon(callback)
        await done

    asyncio.run(main())
    assert ids == [None]


def test_one_task_keeps_one_id_across_records():
    """Grouping by task is only meaningful if the id holds for the task's life."""
    ids: list[int | None] = []

    async def inner() -> None:
        for _ in range(3):
            ids.append(LogRecordRow.from_log_record(_make_record()).asyncio_task_id)
            await asyncio.sleep(0)

    asyncio.run(inner())
    assert len(set(ids)) == 1


def test_sequential_asyncio_tasks_never_share_an_id():
    """The bug issue #4 was filed for: `id()` returns the object's address, and
    CPython hands the freed address to the next task of the same shape. Two
    tasks that never overlapped in time then merged into one apparent task.
    Measured against the pre-fix code: these 20 runs yielded 5 distinct ids."""
    ids: list[int | None] = []

    async def inner() -> None:
        ids.append(LogRecordRow.from_log_record(_make_record()).asyncio_task_id)

    for _ in range(20):
        asyncio.run(inner())
        gc.collect()

    assert len(set(ids)) == len(ids)


def test_concurrent_asyncio_tasks_get_distinct_ids():
    ids: list[int | None] = []

    async def inner() -> None:
        ids.append(LogRecordRow.from_log_record(_make_record()).asyncio_task_id)
        await asyncio.sleep(0)

    async def main() -> None:
        await asyncio.gather(*(inner() for _ in range(5)))

    asyncio.run(main())
    assert len(set(ids)) == 5
