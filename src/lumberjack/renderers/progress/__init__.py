"""The bar models: what is inferred from log lines, and what was declared.

Three models, all reading the store and none importing `rich`, so the whole
display layer is swappable and testable on a bare install:

* `RepeatingSourceModel` (`sources.py`) — the inferred half. Identity is the
  *source location*: a `logger.debug(...)` inside a loop hits the same line
  every iteration, so `(pathname, lineno, func_name)` groups a loop's ticks
  with no template extraction, no masking and no clustering. On top of that
  grouping it measures each source's period, sorts sources by period to
  recover which loop encloses which, takes the ratio between an enclosing loop
  and an enclosed one as the inner loop's iteration count, and retires a bar
  whose source has gone quiet.
* `SessionHeartbeat` (`heartbeat.py`) — one row for the whole session, and the
  only element that says something about a program whose lines never repeat.
  It rides on `RepeatingSourceModel`'s poll rather than fetching a delta of
  its own.
* `TaskProgressModel` (`tasks.py`) — the exact half. Every number came from a
  `task()` or `track()` call that stated it outright, so nothing here guesses.

Counts come from the store, never from tallying the handler's live callback,
so every renderer reading that store sees the same numbers. The store is read
forward from a watermark rather than re-tallied, so a redraw costs what
arrived since the last one instead of what the store holds — and every
inference here runs per *poll*, over sources rather than rows, so log volume
never drives its cost either.

The three arrived in three eras (Phase 1's counting, 4a's task bars, 4b's
inference) and shared one module until they shared nothing but the
`RecordStore` interface. One module each now, with `smoothing.py` holding the
interval fold both the period and the arrival rate use. This package is that
module's name, so `lumberjack.renderers.progress` still imports every one of
them and there is no second surface to keep in step.
"""

from __future__ import annotations

from lumberjack.renderers.progress.heartbeat import (
    HEARTBEAT_FRAMES,
    MESSAGE_LOOKBACK,
    HeartbeatState,
    SessionHeartbeat,
)
from lumberjack.renderers.progress.smoothing import PERIOD_SMOOTHING
from lumberjack.renderers.progress.sources import (
    CONTAINMENT_CONFIRMATIONS,
    DEFAULT_MIN_REPEATS,
    DEFAULT_REFRESH_INTERVAL,
    IDLE_PERIODS,
    MAX_BARS_ENV_VAR,
    MIN_IDLE_SECONDS,
    MIN_NESTING_RATIO,
    RATIO_TOLERANCE,
    SAME_LOOP_TOLERANCE,
    BarState,
    RepeatingSourceModel,
    resolve_max_bars,
)
from lumberjack.renderers.progress.tasks import TaskBarState, TaskProgressModel

__all__ = [
    "CONTAINMENT_CONFIRMATIONS",
    "DEFAULT_MIN_REPEATS",
    "DEFAULT_REFRESH_INTERVAL",
    "HEARTBEAT_FRAMES",
    "IDLE_PERIODS",
    "MAX_BARS_ENV_VAR",
    "MESSAGE_LOOKBACK",
    "MIN_IDLE_SECONDS",
    "MIN_NESTING_RATIO",
    "PERIOD_SMOOTHING",
    "RATIO_TOLERANCE",
    "SAME_LOOP_TOLERANCE",
    "BarState",
    "HeartbeatState",
    "RepeatingSourceModel",
    "SessionHeartbeat",
    "TaskBarState",
    "TaskProgressModel",
    "resolve_max_bars",
]
