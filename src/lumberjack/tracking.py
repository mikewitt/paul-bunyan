"""The tracking API: `task()` and `track()`, rung 2 of the value ladder.

Instrumentation that is always present and produces whatever the *application*
has installed and configured — spans if it configured OTel, store records if
it called `init()`, both if both, nothing if neither. See CLAUDE.md Principle
4 for the full table; the point is that a library calling `task()` owns none
of those switches, and so imposes no dependency and no output downstream.

The no-session check in `_emit()` is what makes the "nothing" case real. It
is a runtime degradation, not the call-time dependency Principle 4 forbids:
task events are ordinary `logging` records, so the handler stays the only
writer — but records reach that handler by way of the *root* logger, so
emitting unconditionally would print a library's instrumentation into any
host application that had configured logging at all.
lumberjack: see issue #31 for the opt-in version.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import logging
import sys
import threading
import time
from collections.abc import Sized
from typing import TYPE_CHECKING, Self

from lumberjack import otel
from lumberjack.schema import EXTRA_KEY, TaskEvent, TaskEventKind
from lumberjack.session import current_session

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from types import TracebackType

    from opentelemetry.trace import Span

#: Every task event goes to this one logger, so an application layering with
#: `init(replace_handlers=False)` can route or silence the lot with a single
#: `logging.getLogger(...)` call.
TASK_LOGGER_NAME = "lumberjack.task"

#: Smallest gap between progress ticks, and a shipping constraint rather than
#: a tuning knob.
#:
#: The unconditional reason: non-TTY output is write-through, one line per
#: record, so a record per `advance()` prints a million lines for a
#: million-item loop — the disease this package exists to cure, caused by the
#: cure. That holds on every machine and needs no measurement.
#:
#: The second reason is real but machine-dependent, so take the number with
#: its conditions rather than as a law: an unsampled loop measured here at
#: ~59,000 records/s through the handler alone and ~24,000 with the
#: JSON-lines renderer attached, against a buffer that drains 10,000 per
#: 200ms — i.e. 50,000/s. Fast enough hardware therefore overruns lumberjack's
#: own buffer and trips its own dropped-records warning; slower hardware, or a
#: heavier renderer, does not. Re-measure before quoting a figure.
TICK_INTERVAL = 0.05

#: `(pathname, lineno, func_name)` of the user code that opened a task.
Origin = tuple[str, int, str]

_task_ids = itertools.count(1)

#: The task a bare `task()` call nests under. Only `__enter__` sets it and
#: only `__exit__` clears it — but read it through `_ambient_parent()`, never
#: directly: resets are not guaranteed to arrive in order.
_current_task: contextvars.ContextVar[TaskHandle | None] = contextvars.ContextVar(
    "lumberjack_current_task", default=None
)


def _ambient_parent() -> TaskHandle | None:
    """The nearest *unfinished* task in the ambient chain, or None.

    Skipping finished handles is what keeps the answer right when the
    contextvar tokens were reset out of order — see `TaskHandle._unbind()`.
    A finished task must never become a parent.

    The walk follows `_prev_ambient`, the handle that was ambient when this
    one was *entered* — not `_parent`, which is where it was *created*. Those
    differ: a handle built at module scope and entered inside some other
    task has no creation-time link to it, so walking `_parent` would step
    straight past a task whose `with` block is still open and orphan the new
    one. Everything reachable through the ambient slot was entered, so
    `_prev_ambient` is always set on it.
    """
    candidate = _current_task.get()
    while candidate is not None and candidate._ended:  # noqa: SLF001 - same class
        candidate = candidate._prev_ambient  # noqa: SLF001 - same class
    return candidate


def _caller_origin() -> Origin:
    """Where the caller of the public entry point that called us lives.

    The depth is fixed at 2 because every entry point calls this *directly*.
    Routing it through a shared helper — or letting `subtask()` delegate to
    `task()` — would attribute every call site in the program to one line of
    lumberjack's own source, collapsing them into a single bar.

    `stacklevel=` cannot do this job: the depth differs per entry point, and
    `__exit__` is invoked by the interpreter rather than from user code.
    """
    frame = sys._getframe(2)  # noqa: SLF001 - no public API for a caller's frame
    return frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name


class TaskHandle:
    """A running task. Returned by `task()`; also its own context manager.

    Use it with `with`. A handle used bare has a parent but never becomes
    one — only `__enter__` establishes ambient parentage — so `subtask()`,
    which takes its parent as `self`, is how nesting works off the main
    thread. `contextvars` propagate into asyncio tasks but *not* into a bare
    `threading.Thread`, so a worker thread has to be handed its handle.

    Entering is single-threaded by contract: `__enter__`/`__exit__` belong to
    one `with`, and a handle entered from two threads at once is rejected
    rather than serialized. Counting is not — `advance()` from many threads
    is fine.

    Two documented surprises:

    * Under `init(replace_handlers=False)` the application asked to layer, so
      task events reach the store *and* the application's own handlers.
    * A handle that is never ended — dropped, or the process exits mid-task —
      leaves a `start` row with no `end` row. Readers must tolerate that.
      lumberjack: see issue #33 (sweeping them at exit).
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
        self._parent = parent
        self.parent_task_id = parent.task_id if parent is not None else None
        self._level = level
        self._origin = origin
        # Guards the counters, the `_ended`/`_started` flags, and emission.
        # Cross-thread use is a first-class case, and an unsynchronized
        # `+= 1` would silently under-report. `_token`/`_otel_token` are
        # *not* under it: they belong to `with`, which is single-threaded by
        # construction — a handle entered from two threads at once is a bug
        # `__enter__` rejects rather than serializes.
        self._lock = threading.Lock()
        self._current = 0
        # Unvalidated: `total` is annotated `int | None` and nothing
        # enforces it, so `inf` reaches the display as a determinate
        # bar stuck at 0% forever.
        # lumberjack: see issue #98
        self._total = total
        self._ended = False
        # Whether the `start` row was actually written — see `_emit()`, which
        # is where the all-or-nothing rule this flag carries is explained.
        self._started = False
        self._token: contextvars.Token[TaskHandle | None] | None = None
        #: What was ambient when this handle was *entered*. `_ambient_parent()`
        #: walks this, not `_parent` — see there.
        self._prev_ambient: TaskHandle | None = None
        self._otel_token: object | None = None
        # Not 0.0: that only makes the first tick fire because
        # `time.monotonic()` happens to count from boot on CPython's main
        # platforms, and its epoch is documented as undefined.
        self._last_tick = float("-inf")
        # Both the span and the start row are opened here rather than in
        # `__enter__`: a handle exists from the moment it is created, whether
        # or not it is entered. Making the span current is the part that
        # belongs to `__enter__`, below.
        self._span: Span | None = None
        tracer = otel.tracer()
        if tracer is not None:
            parent_span = parent._span if parent is not None else None  # noqa: SLF001
            self._span = tracer.start_span(
                label, context=otel.context_with_span(parent_span)
            )
        self._emit("start", self._current, self._total)

    def __enter__(self) -> Self:
        # Check and claim under the lock. Unsynchronized this is a
        # check-then-act: two threads entering the same handle both passed
        # the test and both "entered", one silently clobbering the other's
        # token — measured at 3 occurrences in 2000 attempts on a GIL build,
        # and the window is wider without one.
        with self._lock:
            if self._token is not None or self._ended:
                raise RuntimeError(f"task {self.label!r} cannot be entered twice")
            self._prev_ambient = _current_task.get()
            self._token = _current_task.set(self)
        if self._span is not None:
            self._otel_token = otel.attach(self._span)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Released before `end()`, which takes the same non-reentrant lock.
        with self._lock:
            token, self._token = self._token, None
        otel_token, self._otel_token = self._otel_token, None
        try:
            # `GeneratorExit` is control flow, not failure: it is what a
            # user's generator gets when the consumer stops early, and
            # `track()`'s own early `break` records a clean end. Treating it
            # as an error would put a traceback on the ERROR channel — the
            # one line a user actually needs to see — for a normal `break`,
            # and would mark the span failed.
            self.end(None if isinstance(exc, GeneratorExit) else exc)
        finally:
            otel.detach(otel_token)
            self._unbind(token)

    def _unbind(self, token: contextvars.Token[TaskHandle | None] | None) -> None:
        """Give the ambient slot back, without trusting the token blindly.

        `with` is LIFO within one frame, but two suspended generators each
        holding one interleave freely — and `ContextVar.reset()` does *not*
        raise for an out-of-order token from the same Context. It silently
        writes `token.old_value` back, which would leave the ambient slot
        pointing at a handle that has already finished. `track()` hands back
        generators, so this is reachable, not theoretical.

        Two guards, and both are needed. Resetting only while still the
        current binding stops an inner handle's exit from evicting an outer
        one that is still open; `_ambient_parent()` then skips any finished
        handle the restored chain lands on.
        """
        if token is None:
            return
        if _current_task.get() is not self:
            # A later handle is still ambient. Its own exit restores the
            # chain; ours would evict a live task.
            return
        # `ValueError` here means the handle was entered in one
        # `contextvars.Context` and exited in another: reachable by entering
        # here and exiting inside an asyncio task, which runs on a copy.
        # Raising out of a `finally` would replace the user's in-flight
        # exception with our bookkeeping error, and there is nothing to
        # repair — the copy dies with the task.
        with contextlib.suppress(ValueError):
            _current_task.reset(token)

    def subtask(self, name: str, *, total: int | None = None) -> TaskHandle:
        """A child task parented to this one explicitly.

        `self` is the parent, not whatever the ambient context says, so this
        works from a worker thread no contextvar reached. Captures its own
        frame rather than delegating to `task()` — see `_caller_origin()`.
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

        **Ticks are sampled** — at most one record per `TICK_INTERVAL` (see
        there for why that is mandatory rather than a tuning knob). Any task
        that ends reports an exact final count, because `progress_current` is
        absolute and `end()` writes it unsampled. A task that advances and
        then stalls shows its last sampled value until it does.
        """
        self._bump(n, None, absolute=False)

    def set_progress(self, current: int, total: int | None = None) -> None:
        """Set the absolute count (and optionally the total). Sampled, as
        `advance()` is."""
        self._bump(current, total, absolute=True)

    def end(self, exc: BaseException | None = None) -> None:
        """Emit the final record and close the span. Idempotent — `__exit__`
        relies on that.

        The `end` record carries the final `progress_current`, so it doubles
        as the unsampled last tick. Progress lands on the span once, here,
        never per tick.
        """
        with self._lock:
            if self._ended:
                return
            self._ended = True
            current, total = self._current, self._total
            level = logging.ERROR if exc is not None else self._level
            # Emitted under the lock so an `update` still in flight on
            # another thread cannot land *after* this row. Readers treat
            # `end` as terminal, and the whole defence of sampling is that
            # this row carries the true final count.
            self._emit("end", current, total, level=level, exc=exc)
        if self._span is not None:
            self._span.set_attribute("lumberjack.progress.current", current)
            if total is not None:
                self._span.set_attribute("lumberjack.progress.total", total)
            if exc is not None:
                otel.record_failure(self._span, exc)
            self._span.end()

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
            self._emit("update", self._current, self._total)

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
        # Rows are all-or-nothing per task: filtering is per record and
        # `end()` promotes to ERROR, so without this a task under
        # `init(level=WARNING)` would write a lone `end` row with no `start`
        # to anchor it — and a reader has no way to tell that from a task
        # whose start it simply missed.
        if kind == "start":
            self._started = True
        elif not self._started:
            return
        logger = logging.getLogger(TASK_LOGGER_NAME)
        level = self._level if level is None else level
        if not logger.isEnabledFor(level):
            if kind == "start":
                self._started = False
            return
        pathname, lineno, func_name = self._origin
        record = logger.makeRecord(
            TASK_LOGGER_NAME,
            level,
            pathname,
            lineno,
            self._message(kind, current, total, exc),
            (),
            None if exc is None else (type(exc), exc, exc.__traceback__),
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
        progress = str(current) if total is None else f"{current}/{total}"
        if kind == "start":
            return f"task start: {self.label}"
        if kind == "update":
            return f"task progress: {self.label} {progress}"
        # The count belongs on the end row too. `PlainTextRenderer` prints
        # `message` and nothing else, so the non-TTY user — the one getting
        # write-through text rather than a bar — would otherwise never see
        # the final count that justifies sampling the ticks.
        if exc is not None:
            return f"task failed: {self.label} {progress}: {exc!r}"
        return f"task end: {self.label} {progress}"


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

    INFO rather than DEBUG because a task boundary is not debug spam: it is
    the thing the caller went out of their way to state. A quieter capture —
    `init(level=logging.INFO)`, or a host application that configured logging
    itself — must still see it, and DEBUG is the level such a configuration
    drops first.

    `_origin` is internal — `track()` hands its own frame down. See
    `_caller_origin()`.
    """
    return TaskHandle(
        label=name,
        level=level,
        origin=_origin if _origin is not None else _caller_origin(),
        total=total,
        parent=_ambient_parent(),
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
    whoever iterates it and delay its start until then.
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
