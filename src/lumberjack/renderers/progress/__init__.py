"""The bar models: what is inferred from log lines, and what was declared.

Every model here reads the store and none imports `rich`, so the whole display
layer is swappable and testable on a bare install.

The **identity** layer — one entry per source location, which is what was
captured and what the store would corroborate:

* `RepeatingSourceModel` (`sources.py`) — the inferred half. Identity is the
  *source location*: a `logger.debug(...)` inside a loop hits the same line
  every iteration, so `(pathname, lineno, func_name)` groups a loop's ticks
  with no template extraction, no masking and no clustering. On top of that
  grouping it measures each source's period, sorts sources by period to
  recover which loop encloses which, takes the ratio between an enclosing loop
  and an enclosed one as the inner loop's iteration count, and retires a bar
  whose source has gone quiet.
* `TaskProgressModel` (`tasks.py`) — the exact half. Every number came from a
  `task()` or `track()` call that stated it outright, so nothing here guesses.

The **display** layer, which is a different question and used to be answered by
accident, because grouping by source location already produced something a
renderer could draw:

* `LoopRowModel` (`loops.py`) — one row per *loop* rather than per call site,
  static structure first and period-and-worker second, counting iterations
  rather than records. It groups what `RepeatingSourceModel` computed and
  reaches into none of it.
* `CyclePositionModel` (`position.py`) — the second row a loop may earn:
  where the current iteration has got to within the body, read off the AST's
  ordering of the call sites and drawn only when the loop row above it ticks
  too slowly to answer "is this still running?".
* `SessionHeartbeat` (`heartbeat.py`) — one row for the whole session, and the
  only element that says something about a program whose lines never repeat.
  It rides on `RepeatingSourceModel`'s poll rather than fetching a delta of
  its own.
* `layout.py` — the structural order rows are drawn in, and `templates.py` the
  labels they are drawn with.

Counts come from the store, never from tallying the handler's live callback,
so every renderer reading that store sees the same numbers. The store is read
forward from a watermark rather than re-tallied, so a redraw costs what
arrived since the last one instead of what the store holds — and every
inference here runs per *poll*, over sources rather than rows, so log volume
never drives its cost either.

They arrived in four eras (Phase 1's counting, 4a's task bars, 4b's inference,
then the row model) and shared one module until they shared nothing but the
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
from lumberjack.renderers.progress.layout import depth_first_order
from lumberjack.renderers.progress.loops import LoopRow, LoopRowModel
from lumberjack.renderers.progress.position import (
    MIN_BODY_SITES,
    MIN_LEGIBLE_PERIOD,
    CyclePosition,
    CyclePositionModel,
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
from lumberjack.renderers.progress.templates import (
    LOOKBACKS,
    MAX_LABEL,
    TemplateIndex,
    describe_template,
)

__all__ = [
    "CONTAINMENT_CONFIRMATIONS",
    "DEFAULT_MIN_REPEATS",
    "DEFAULT_REFRESH_INTERVAL",
    "HEARTBEAT_FRAMES",
    "IDLE_PERIODS",
    "LOOKBACKS",
    "MAX_BARS_ENV_VAR",
    "MAX_LABEL",
    "MESSAGE_LOOKBACK",
    "MIN_BODY_SITES",
    "MIN_IDLE_SECONDS",
    "MIN_LEGIBLE_PERIOD",
    "MIN_NESTING_RATIO",
    "PERIOD_SMOOTHING",
    "RATIO_TOLERANCE",
    "SAME_LOOP_TOLERANCE",
    "BarState",
    "CyclePosition",
    "CyclePositionModel",
    "HeartbeatState",
    "LoopRow",
    "LoopRowModel",
    "RepeatingSourceModel",
    "SessionHeartbeat",
    "TaskBarState",
    "TaskProgressModel",
    "TemplateIndex",
    "depth_first_order",
    "describe_template",
    "resolve_max_bars",
]
