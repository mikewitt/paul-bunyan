"""Periodic buffer→store drain.

`LumberjackHandler.emit()` deliberately only appends to an in-memory deque, so
without something driving `flush()` the store stays empty until process exit.
That starves every store *reader* — repetition analysis and any renderer that
redraws from stored state rather than from the live record callback.

This is a timer, not a record counter: drain cadence is decoupled from log
volume, so a burst of a million records costs the same number of store writes
per second as a trickle.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable

DEFAULT_FLUSH_INTERVAL = 0.2


class FlushPump:
    """Daemon thread that calls `flush` every `interval` seconds.

    Named for the buffer→store job, but `RichProgressRenderer` uses it as its
    redraw timer too — it is really a generic periodic timer.
    lumberjack: see issue #16
    """

    def __init__(
        self, *, interval: float, flush: Callable[[], None], name: str | None = None
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.interval = interval
        self._flush = flush
        self._name = name or "lumberjack-flush"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start pumping. Idempotent."""
        if self._thread is not None:
            return
        self._stop.clear()
        # Daemon: a stalled pump must never wedge interpreter shutdown. The
        # atexit hook drains whatever is left anyway.
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # wait() doubles as an interruptible sleep: stop() is never left
        # waiting out a full interval.
        while not self._stop.wait(self.interval):
            # A store write failing must not kill the pump: the next tick
            # still runs. It does not re-deliver the rows that failed, though —
            # `drain()` empties the buffer before `append()` is attempted.
            with contextlib.suppress(Exception):
                self._flush()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread and wait for it to exit. Idempotent."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
