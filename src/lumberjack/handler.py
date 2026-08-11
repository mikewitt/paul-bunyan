"""logging.Handler that captures records into a bounded write buffer."""

from __future__ import annotations

import collections
import logging
from collections.abc import Callable

from lumberjack.schema import LogRecordRow

DEFAULT_BUFFER_SIZE = 10_000


class LumberjackHandler(logging.Handler):
    """Captures records with attribution into a bounded deque.

    Store writes are batched separately (via `drain()`), so `emit()` stays
    cheap and never touches the store directly. `on_record`, if given, is
    invoked synchronously per record for write-through renderers.
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
        self.on_record = on_record

    def emit(self, record: logging.LogRecord) -> None:
        try:
            row = LogRecordRow.from_log_record(record)
        except Exception:
            self.handleError(record)
            return

        with self.lock:
            self._buffer.append(row)

        if self.on_record is not None:
            self.on_record(row)

    def drain(self) -> list[LogRecordRow]:
        """Atomically empty and return the buffer, oldest first."""
        with self.lock:
            rows = list(self._buffer)
            self._buffer.clear()
        return rows

    def peek(self, n: int | None = None) -> list[LogRecordRow]:
        """Non-destructive snapshot of the buffer, oldest first."""
        with self.lock:
            rows = list(self._buffer)
        if n is None:
            return rows
        return rows[-n:]
