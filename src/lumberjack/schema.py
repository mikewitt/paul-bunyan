"""Record schema: the shape lumberjack stores for every captured log record.

Derived from stdlib `logging.LogRecord`'s standard attributes, plus
lumberjack's own attribution columns (asyncio task, and reserved slots for
task hierarchy / template clustering that later phases populate).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import NamedTuple


@dataclasses.dataclass(frozen=True, slots=True)
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

    # lumberjack attribution
    task_name: str | None
    task_id: int | None
    parent_task_id: int | None
    template_id: int | None

    @classmethod
    def from_log_record(cls, record: logging.LogRecord) -> LogRecordRow:
        exc_text = record.exc_text
        if exc_text is None and record.exc_info:
            # lumberjack: see issue #18 (formatter could be a singleton)
            exc_text = logging.Formatter().formatException(record.exc_info)

        task_id: int | None = None
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is not None:
            # id() is reused after GC, so sequential tasks can collide.
            # lumberjack: see issue #4
            task_id = id(task)

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
            task_name=getattr(record, "taskName", None),
            task_id=task_id,
            parent_task_id=None,
            template_id=None,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class StoredRecord(LogRecordRow):
    id: int


class SourceKey(NamedTuple):
    pathname: str
    lineno: int
    func_name: str
