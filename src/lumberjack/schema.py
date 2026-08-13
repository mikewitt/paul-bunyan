"""Record schema: the shape lumberjack stores for every captured log record.

Derived from stdlib `logging.LogRecord`'s standard attributes, plus
lumberjack's own attribution columns (asyncio task, and reserved slots for
task hierarchy / template clustering that later phases populate).
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
from typing import Literal, NamedTuple

#: The single `extra=` key the tracking API attaches its payload under.
#: Verified not to collide with any existing `LogRecord` attribute — unlike
#: `taskName`, which does exist and is why asyncio attribution had to be
#: renamed.
EXTRA_KEY = "lumberjack"

_asyncio_task_ids = itertools.count(1)
_TASK_ID_ATTR = "_lumberjack_task_id"


def _current_asyncio_task_id() -> int | None:
    """A collision-free id for the running asyncio task, or None outside one.

    `id()` is not usable: it is the object's address, and CPython hands the
    address of a collected task to the next one of the same shape, so two
    tasks that never overlapped in time merge into one apparent task. The id
    comes from a counter instead, cached on the task object so it stays stable
    for that task's life. lumberjack: closes issue #4.

    No lock: `itertools.count.__next__` is a single C call, and
    `asyncio.current_task()` only ever returns a task owned by the calling
    thread's loop, so one thread ever writes a given task's attribute.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    if task is None:
        return None
    task_id: int | None = getattr(task, _TASK_ID_ATTR, None)
    if task_id is None:
        task_id = next(_asyncio_task_ids)
        # `_asyncio.Task` accepts instance attributes despite exposing no
        # `__dict__`; a `__slots__` subclass inherits that same storage.
        setattr(task, _TASK_ID_ATTR, task_id)
    return task_id


#: Where a task event sits in the task's life. Stored as text.
TaskEventKind = Literal["start", "update", "end"]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class TaskEvent:
    """What the tracking API attaches to a `LogRecord`, under one `extra=` key.

    One typed key rather than four loose `extra` names: one collision surface
    instead of four, and the `isinstance` check at the read end means a foreign
    `record.lumberjack` from some other library degrades to "no task data"
    rather than crashing `emit()` for every record in the process.
    """

    label: str
    kind: TaskEventKind
    task_id: int
    parent_task_id: int | None = None
    current: int | None = None
    total: int | None = None


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class LogRecordRow:
    logger_name: str
    level_name: str
    level_no: int
    msg: str
    message: str
    pathname: str
    filename: str
    module: str
    func_name: str
    lineno: int
    created: float
    thread: int
    thread_name: str
    process: int
    process_name: str
    exc_text: str | None
    stack_text: str | None

    # lumberjack attribution. `asyncio_*` is where the record ran; `task_id` /
    # `parent_task_id` are the tracking API's, and stay None for records it
    # did not emit.
    asyncio_task_name: str | None
    asyncio_task_id: int | None
    task_id: int | None
    parent_task_id: int | None

    # Progress, as four columns rather than a blob: structure at write time
    # (Principle 3), and `progress_current`/`progress_total` are the two
    # numbers a determinate bar reads. A JSON blob would need JSON1, which
    # does not carry over to DuckDB.
    task_label: str | None
    task_event: TaskEventKind | None
    progress_current: int | None
    progress_total: int | None

    template_id: int | None

    @classmethod
    def from_log_record(cls, record: logging.LogRecord) -> LogRecordRow:
        exc_text = record.exc_text
        if exc_text is None and record.exc_info:
            # lumberjack: see issue #18 (formatter could be a singleton)
            exc_text = logging.Formatter().formatException(record.exc_info)

        # A foreign attribute under our key is somebody else's, not a bug to
        # raise on: ignore it and store the record without task data.
        event = getattr(record, EXTRA_KEY, None)
        if not isinstance(event, TaskEvent):
            event = None

        return cls(
            logger_name=record.name,
            level_name=record.levelname,
            level_no=record.levelno,
            msg=str(record.msg),
            message=record.getMessage(),
            pathname=record.pathname,
            filename=record.filename,
            module=record.module,
            func_name=record.funcName,
            lineno=record.lineno,
            created=record.created,
            thread=record.thread or 0,
            thread_name=record.threadName or "",
            process=record.process or 0,
            process_name=record.processName or "",
            exc_text=exc_text,
            stack_text=record.stack_info,
            asyncio_task_name=getattr(record, "taskName", None),
            asyncio_task_id=_current_asyncio_task_id(),
            task_id=event.task_id if event else None,
            parent_task_id=event.parent_task_id if event else None,
            task_label=event.label if event else None,
            task_event=event.kind if event else None,
            progress_current=event.current if event else None,
            progress_total=event.total if event else None,
            template_id=None,
        )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class StoredRecord(LogRecordRow):
    id: int


class SourceKey(NamedTuple):
    pathname: str
    lineno: int
    func_name: str
