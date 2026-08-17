"""logging.Handler that captures records into a bounded write buffer."""

from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable

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

    def emit(self, record: logging.LogRecord) -> None:
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
        """Atomically empty and return the buffer, oldest first."""
        with self._lock:
            rows = list(self._buffer)
            self._buffer.clear()
        return rows
