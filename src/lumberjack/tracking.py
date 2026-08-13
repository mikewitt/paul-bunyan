"""The tracking API: `task()` and `track()`, rung 2 of the value ladder.

Instrumentation that is always present and produces whatever the *application*
has installed and configured — nothing otherwise. Three independent axes:

===========================================  ===============================
App has                                      `task()` produces
===========================================  ===============================
bare lumberjack, no `init()`                 nothing
OTel configured normally                     OTel spans
`lumberjack.init()`                          records in the store
===========================================  ===============================

None of them gates the others, and a library calling `task()` imposes no
dependency and no output on its downstream users.

The no-session check below is a runtime degradation, not the "call-time
dependency on `init()`" Principle 4 forbids: nothing *requires* `init()`, and
the no-session path is the documented inert one. It is load-bearing rather
than defensive. Task events become ordinary `logging` records so the handler
stays the only writer (Architecture) — but records reach that handler because
it sits on the *root* logger, so emitting unconditionally would print a
library's instrumentation into any host application that had configured
logging at all. Issue #31 holds the opt-in version of that.
"""

from __future__ import annotations

import contextvars
import itertools
import logging
import sys
import threading
import time
from collections.abc import Sized
from typing import TYPE_CHECKING

from lumberjack.schema import EXTRA_KEY, TaskEvent, TaskEventKind
from lumberjack.session import current_session

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from types import TracebackType

#: Every task event goes to this one logger, so an application layering with
#: `init(replace_handlers=False)` can route or silence the lot with a single
#: `logging.getLogger(...)` call.
TASK_LOGGER_NAME = "lumberjack.task"

#: Smallest gap between progress ticks. Not a tuning knob — see `advance()`.
TICK_INTERVAL = 0.05

#: `(pathname, lineno, func_name)` of the user code that opened a task.
Origin = tuple[str, int, str]

_task_ids = itertools.count(1)

#: The task a bare `task()` call nests under. Only `__enter__` sets it, which
#: is what makes an out-of-order reset structurally impossible.
_current_task: contextvars.ContextVar[TaskHandle | None] = contextvars.ContextVar(
    "lumberjack_current_task", default=None
)


def _caller_origin() -> Origin:
    """Where the caller of the public entry point that called us lives.

    The depth is fixed at 2 because every entry point calls this *directly*.
    Routing it through a shared helper — or letting `subtask()` delegate to
    `task()` — would attribute every call site in the program to one line of
    lumberjack's own source, collapsing them into a single bar.

    `stacklevel=` cannot do this job: the depth differs per entry point, and
    `__exit__` is invoked by the interpreter rather than from user code.
    """
    frame = sys._getframe(2)
    return frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name


class TaskHandle:
    """A running task. Returned by `task()`; also its own context manager.

    Use it with `with`. A handle used bare has a parent but never becomes
    one — only `__enter__` establishes ambient parentage — so `subtask()`,
    which takes its parent as `self`, is how nesting works off the main
    thread. `contextvars` propagate into asyncio tasks but *not* into a bare
    `threading.Thread`, so a worker thread has to be handed its handle.

    Two documented surprises:

    * Under `init(replace_handlers=False)` the application asked to layer, so
      task events reach the store *and* the application's own handlers.
    * A handle that is never ended — dropped, or the process exits mid-task —
      leaves a `start` row with no `end` row. Readers must tolerate that.
      Issue #33 covers sweeping them at exit.
    """

    def __init__(
        self,
        *,
        label: str,
        level: int,
        origin: Origin,
        total: int | None = None,
        parent: TaskHandle | None = None,
    ) -> None:
        self.label = label
        self.task_id = next(_task_ids)
        self.parent_task_id = parent.task_id if parent is not None else None
        self._level = level
        self._origin = origin
        # Guards every mutable field below. Cross-thread use is a first-class
        # case, and an unsynchronized `+= 1` would silently under-report.
        self._lock = threading.Lock()
        self._current = 0
        self._total = total
        self._ended = False
        self._token: contextvars.Token[TaskHandle | None] | None = None
        self._last_tick = 0.0
        # The start row is written here rather than in `__enter__`: a handle
        # exists from the moment it is created, whether or not it is entered.
        self._emit("start", self._current, self._total)

    def __enter__(self) -> TaskHandle:
        if self._token is not None or self._ended:
            raise RuntimeError(f"task {self.label!r} cannot be entered twice")
        self._token = _current_task.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        token, self._token = self._token, None
        try:
            self.end(exc)
        finally:
            # `token is not None` because `__exit__` only follows `__enter__`;
            # the check is for the type checker.
            if token is not None:
                try:
                    _current_task.reset(token)
                except ValueError:
                    # Entered in one `contextvars.Context` and exited in
                    # another. Only reachable by driving `__enter__`/`__exit__`
                    # across an asyncio task boundary by hand — verified that
                    # generators, sync and async, do *not* hit this. Raising
                    # out of a `finally` would replace the user's in-flight
                    # exception with our own bookkeeping error, so clear the
                    # binding we can see instead.
                    _current_task.set(None)

    def subtask(self, name: str, *, total: int | None = None) -> TaskHandle:
        """A child task parented to this one explicitly.

        `self` is the parent, not whatever the ambient context says, so this
        works from a worker thread no contextvar reached.
        """
        return TaskHandle(
            label=name,
            level=self._level,
            origin=_caller_origin(),
            total=total,
            parent=self,
        )

    def advance(self, n: int = 1) -> None:
        """Report `n` more units of work done. A no-op once the task has ended.

        **Ticks are sampled**: at most one record per `TICK_INTERVAL`, plus the
        `end` record, which carries the final count. That is not an
        optimisation. A tight loop emits ~85,000 records/s against a write
        buffer that drains 10,000 per 200ms, so a record per call overflows
        lumberjack's own buffer and trips its own dropped-records warning —
        and under the non-TTY default the write-through plain renderer would
        print a line per item, which is the disease this package exists to
        cure. Sampling loses nothing, because `progress_current` is absolute
        rather than a delta.
        """
        self._bump(n, None, absolute=False)

    def set_progress(self, current: int, total: int | None = None) -> None:
        """Set the absolute count (and optionally the total). Sampled, as
        `advance()` is."""
        self._bump(current, total, absolute=True)

    def end(self, exc: BaseException | None = None) -> None:
        """Emit the final record. Idempotent — `__exit__` relies on that.

        The `end` record carries the final `progress_current`, so it doubles
        as the unsampled last tick.
        """
        with self._lock:
            if self._ended:
                return
            self._ended = True
            current, total = self._current, self._total
        level = logging.ERROR if exc is not None else self._level
        self._emit("end", current, total, level=level, exc=exc)

    def _bump(self, value: int, total: int | None, *, absolute: bool) -> None:
        now = time.monotonic()
        with self._lock:
            if self._ended:
                return
            self._current = value if absolute else self._current + value
            if total is not None:
                self._total = total
            if now - self._last_tick < TICK_INTERVAL:
                return
            self._last_tick = now
            current, running_total = self._current, self._total
        self._emit("update", current, running_total)

    def _emit(
        self,
        kind: TaskEventKind,
        current: int,
        total: int | None,
        *,
        level: int | None = None,
        exc: BaseException | None = None,
    ) -> None:
        if current_session() is None:
            return
        logger = logging.getLogger(TASK_LOGGER_NAME)
        level = self._level if level is None else level
        if not logger.isEnabledFor(level):
            return
        pathname, lineno, func_name = self._origin
        record = logger.makeRecord(
            TASK_LOGGER_NAME,
            level,
            pathname,
            lineno,
            self._message(kind, current, total, exc),
            (),
            None,
            func=func_name,
            extra={
                EXTRA_KEY: TaskEvent(
                    label=self.label,
                    kind=kind,
                    task_id=self.task_id,
                    parent_task_id=self.parent_task_id,
                    current=current,
                    total=total,
                )
            },
        )
        logger.handle(record)

    def _message(
        self,
        kind: TaskEventKind,
        current: int,
        total: int | None,
        exc: BaseException | None,
    ) -> str:
        # Contract, not debug text: this is what the plain and JSON-lines
        # renderers print. Preformatted rather than a `%`-template with args,
        # since the structured columns already carry the same data.
        if kind == "start":
            return f"task start: {self.label}"
        if kind == "update":
            progress = str(current) if total is None else f"{current}/{total}"
            return f"task progress: {self.label} {progress}"
        if exc is not None:
            return f"task failed: {self.label}: {exc!r}"
        return f"task end: {self.label}"


def task(
    name: str,
    *,
    level: int = logging.INFO,
    total: int | None = None,
    _origin: Origin | None = None,
) -> TaskHandle:
    """Open a named task. Use it with `with`::

        with lumberjack.task("reindex", total=len(docs)) as t:
            for doc in docs:
                t.advance()

    Mirrors an OTel span. Nests under whatever task is ambient at the call
    site; off the main thread, use the parent's `.subtask()` instead.

    INFO rather than DEBUG because `init()` defaults to INFO, and an emission
    the default filters out does not exist as far as a first run is concerned.

    `_origin` is internal: `track()` passes its own captured frame down so the
    task is attributed to the `track()` call site rather than to this module.
    """
    return TaskHandle(
        label=name,
        level=level,
        origin=_origin if _origin is not None else _caller_origin(),
        total=total,
        parent=_current_task.get(),
    )


def track[T](
    iterable: Iterable[T],
    *,
    name: str | None = None,
    level: int = logging.INFO,
    total: int | None = None,
) -> Iterator[T]:
    """Wrap an iterable so iterating it reports progress. Mirrors `tqdm`::

        for doc in lumberjack.track(docs, name="reindex"):
            index(doc)

    `total` is taken from `len()` when the iterable is `Sized` and no total
    was given. A bare generator has no length and is never consumed to find
    one, so it reports an indeterminate count.

    Ticks are sampled — see `TaskHandle.advance()`. The count stays exact
    regardless, because the `end` record carries the final absolute value.

    A plain function, not a generator function: a generator function's body
    does not run until the first `next()`, which would attribute the task to
    whoever iterates it rather than to this call site, and would delay the
    task's start until then.
    """
    if total is None and isinstance(iterable, Sized):
        total = len(iterable)
    handle = task(
        name if name is not None else _describe(iterable),
        level=level,
        total=total,
        _origin=_caller_origin(),
    )
    return _ticking(iterable, handle)


def _ticking[T](iterable: Iterable[T], handle: TaskHandle) -> Iterator[T]:
    # `finally` rather than a `with`: an early `break` closes the generator
    # with `GeneratorExit`, and the task has to end on that path too.
    try:
        for item in iterable:
            yield item
            handle.advance()
    finally:
        handle.end()


def _describe(iterable: Iterable[object]) -> str:
    """A label for an unnamed `track()`. The type name is the only thing
    generically available, and it beats an empty bar."""
    return type(iterable).__name__
