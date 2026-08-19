"""logging.Handler that captures records into a bounded write buffer."""

from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable, Sequence
from typing import override

from lumberjack.schema import LogRecordRow

DEFAULT_BUFFER_SIZE = 10_000


class LumberjackHandler(logging.Handler):
    """Captures records with attribution into a bounded deque.

    Store writes are batched separately (via `drain()`), so `emit()` stays
    cheap and never touches the store directly. `on_record`, if given, is
    invoked synchronously per record for write-through renderers.

    The buffer is bounded, so a burst that outruns the flush pump evicts the
    oldest rows before they ever reach the store. That breaks the losslessness
    the store promises, so it is counted rather than passed over in silence —
    `dropped` is the running total, and teardown reports it at exit.
    """

    def __init__(
        self,
        *,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        on_record: Callable[[LogRecordRow], None] | None = None,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self._buffer: collections.deque[LogRecordRow] = collections.deque(
            maxlen=buffer_size
        )
        # `deque.maxlen` is `int | None` to a type checker and never None
        # here; `restore()` does capacity arithmetic and wants the int.
        self._buffer_size = buffer_size
        # Our own lock, not `logging.Handler.lock`. That one serializes
        # `emit()` against the handler's output; this one guards the buffer,
        # which `drain()` touches from the pump thread without going through
        # `handle()` at all. Always taken innermost, so there is no inversion.
        self._lock = threading.Lock()
        self._dropped = 0
        self.on_record = on_record

    @property
    def dropped(self) -> int:
        """Records evicted unread because the buffer was full, since startup.

        Cumulative for the life of the handler — `drain()` does not reset it.
        Any non-zero value means the store is missing records: either the
        pump interval is too long or `buffer_size` is too small for the load.
        """
        with self._lock:
            return self._dropped

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Never raises, whatever the record or a write-through renderer does.

        The only writer in the system, reached from every thread the program
        logs on and from inside a `logging` call that has never heard of
        lumberjack, so either failure is reported through stdlib's own
        `handleError()` rather than handed back to the caller.

        Buffered, not stored: `drain()` moves rows on from here, so the store
        costs the calling thread one conversion and a `deque.append` under a
        lock held for no longer than that. `on_record` is charged to it as
        well, outside that lock — a write-through renderer's line is paid for
        by the call that logged it, where a live bar's redraw is not.
        """
        try:
            row = LogRecordRow.from_log_record(record)
        except Exception:  # noqa: BLE001 - stdlib's own contract for a bad record
            self.handleError(record)
            return

        with self._lock:
            # deque(maxlen=...) discards silently; check before appending,
            # since afterwards the evicted row is simply gone.
            if len(self._buffer) == self._buffer.maxlen:
                self._dropped += 1
            self._buffer.append(row)

        if self.on_record is not None:
            try:
                self.on_record(row)
            except Exception:  # noqa: BLE001
                # A renderer that raises must not take down the `log.info()`
                # that reached it. `Logger.callHandlers` has no catch of its
                # own — stdlib handlers guard their own `emit()` bodies — so
                # without this a broken stderr pipe surfaces as an exception
                # from an ordinary logging call in code that has never heard
                # of lumberjack. The record is already in the buffer by now,
                # so the store still gets it: only the live view is lost,
                # which is the trade Principle 6 asks for.
                self.handleError(record)

    def drain(self) -> list[LogRecordRow]:
        """Atomically empty and return the buffer, oldest first.

        Destructive: once this returns, nothing else holds those rows. A
        caller that cannot guarantee the write succeeds owes them `restore()`
        on failure, or Principle 6's lossless half is broken silently.
        """
        with self._lock:
            rows = list(self._buffer)
            self._buffer.clear()
        return rows

    def restore(self, rows: Sequence[LogRecordRow]) -> None:
        """Put a failed batch back at the front, oldest first.

        `drain()` empties the buffer before the store write is attempted, so
        a raising `append()` used to lose the batch outright — not the
        counted, announced loss the bounded buffer already makes, but a
        silent one, which is the failure Principle 6 names specifically
        (issue #77).

        Three things this has to keep true, and each shapes the code:

        - **No reordering.** These rows are older than anything that arrived
          while the write was failing, so they go at the *front*.
        - **No unbounded growth.** A permanently failing store — disk full, a
          closed connection — must not grow the retry queue without limit, so
          the batch is trimmed to what fits under the existing ceiling.
        - **The loss stays counted.** What no longer fits increments
          `dropped`, so `teardown._report_dropped()` covers it at exit for
          free rather than needing a second counter with a second message.

        Trimmed from the *front* rather than by letting `extendleft` spill:
        a full `deque` discards from the far end, which would evict the
        newest records the buffer holds in order to make room for the oldest
        ones being retried. Dropping the oldest is the same rule the buffer
        already applies when it overflows.

        **No double-write**, which this cannot enforce and depends on:
        `RecordStore.append()` is all-or-nothing per batch, so a batch that
        raised wrote nothing and replaying it duplicates nothing.
        """
        with self._lock:
            capacity = self._buffer_size - len(self._buffer)
            overflow = len(rows) - capacity
            if overflow > 0:
                self._dropped += overflow
                rows = rows[overflow:]
            self._buffer.extendleft(reversed(rows))
