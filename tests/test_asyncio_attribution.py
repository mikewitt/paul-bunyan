"""Asyncio task-id attribution (issue #4).

`LogRecordRow.asyncio_task_id` has to identify a *running* asyncio task
uniquely and stably for its life, across sequential runs, concurrent tasks in
one loop, and independent loops in separate threads. `id()` looked like the
obvious source and was the bug: it is the object's address, and CPython hands
a freed task's address to the next task of the same shape, so two tasks that
never overlapped in time merged into one apparent task. This module pins the
counter-based fix against every shape that distinction depends on.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading

from lumberjack.schema import LogRecordRow


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


def test_task_attribution_inside_asyncio_task():
    captured: dict[str, LogRecordRow] = {}

    async def inner() -> None:
        record = _make_record(msg="in task", args=())
        captured["row"] = LogRecordRow.from_log_record(record)

    asyncio.run(inner())
    row = captured["row"]
    assert row.asyncio_task_id is not None


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


def test_tasks_in_separate_event_loops_get_distinct_ids():
    """The counter is process-wide but the loops are not. Two threads each
    running their own loop is the shape Principle 3 exists for, and the one
    a single-loop test cannot speak to."""
    ids: list[int | None] = []
    guard = threading.Lock()
    threads, per_loop = 6, 10

    async def inner() -> None:
        task_id = LogRecordRow.from_log_record(_make_record()).asyncio_task_id
        with guard:
            ids.append(task_id)
        await asyncio.sleep(0)

    async def main() -> None:
        await asyncio.gather(*(inner() for _ in range(per_loop)))

    workers = [
        threading.Thread(target=lambda: asyncio.run(main())) for _ in range(threads)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert len(ids) == threads * per_loop
    assert len(set(ids)) == len(ids)
