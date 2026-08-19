"""What one redraw was told to draw, decided before anything is drawn.

`RichProgressRenderer` used to decide *and* paint in the same four methods,
which made the decisions unreachable except by rendering a frame and reading
the text back. That is a poor place to assert from — a percentage and a pulse
render byte-identically once rich clamps `completed > total` — and it means a
second renderer has to re-derive every decision rather than read it.

So the deciding half lives here, as `plan_frame()`: given what the models
already returned, produce one frozen `Frame` describing every row, its label,
its numbers and its state. `rich_renderer.py` becomes a mapper from a `Frame`
onto rich `Task`s, and a test recorder consumes the same `Frame`. Both read
one answer instead of computing two, which is the only structure in which a
recorder cannot silently disagree with the display.

**This module never imports `rich`,** so the whole of the display's policy is
computable on the bare install, where today none of it is exercised.

## What it deliberately does not decide

Six things live on the rich side, because they are properties of the terminal
or of rich's own object model rather than of the frame:

- **Glyph choice.** `heartbeat_frames()` and `ascii_fallback()` read
  `Console.encoding` and `legacy_windows`. `plan_frame()` takes the *resolved*
  strings as arguments instead.
- **Withdrawing a total.** `Progress.update(total=None)` is a no-op by rich's
  documented contract, so `_set_total` reaches into `_tasks`. A `Frame` says
  `total=None`; making that reach the screen is the mapper's job.
- **Re-layout.** Reordering tasks without recreating them preserves the
  elapsed clock, and only rich's `_tasks` dict has the ordering.
- **The elapsed clock** itself, which rich latches in `finished_time`.
- **One `Live` and two unstarted `Progress` objects.** A second *started*
  `Progress` becomes `_nested` and draws nothing while every `Task` still
  holds correct state — so even a parity check is blind to it, and only a
  frame-text assertion catches it.
- **Cropping.** `vertical_overflow="ellipsis"` is rich's.

Withdrawal and re-layout are covered by `tests/test_display_parity.py`. The
other four — glyph choice, the elapsed clock, the one-`Live` rule and cropping
— only by the rich-gated frame tests, which is why none of those may be
thinned. (This paragraph said "the first three" and named glyph choice among
them; `test_display_parity.py` does not mention encoding at all.)
"""

from __future__ import annotations

import dataclasses
import enum
from typing import TYPE_CHECKING

from lumberjack.schema import SourceKey

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lumberjack.renderers.progress import (
        CyclePosition,
        HeartbeatState,
        LoopRow,
        TaskBarState,
    )


#: How wide the heartbeat's count-and-rate cell is padded to, so the message
#: beside it holds one column instead of shuffling sideways every time the
#: count gains a digit. A floor rather than a ceiling: a session busy enough
#: to outgrow it pushes the message right rather than losing any of it.
#:
#: A constant and not a `plan_frame()` argument, unlike `frames` and
#: `separator`. Those two are what the *terminal* can encode and only a
#: renderer holding a console can resolve them. This is a layout decision,
#: which is the plan's own business — and passing it in meant two callers
#: naming the same number, which is a drift waiting to happen.
HEARTBEAT_SUMMARY_WIDTH = 26

#: The widest fixed-point second count either rate cell will print before
#: switching to exponential. 11 holds `9,999,999.9` — a hundred and fifteen
#: days an iteration — in full digits; past that the exact seconds say nothing
#: the exponent does not.
#:
#: **The trigger is a clock step, not a corrupted timestamp**, and getting
#: that wrong is what made an earlier version of this bound too loose. A
#: period is folded from raw `record.created` spans with no upper bound, so a
#: container that starts at epoch 0 and then NTP-syncs, a VM resumed from a
#: snapshot, or an unsynchronised RTC all produce one span of ~1.7e9 seconds.
#: That renders `1,755,000,000.0s each` — 21 characters in a column otherwise
#: about 8 — and `_RowTextColumn` sets `no_wrap` with no maximum, so rich
#: measures it at full width and squeezes everything else. Measured at width
#: 80 the bar drops from 34 cells to 20; at width 60, from 14 to 3, with the
#: elapsed clock truncated to `0:0…`.
#:
#: A fabricated `created` near 1e308 is the same defect further out — 419
#: characters — and the same bound closes both.
MAX_SECONDS_WIDTH = 11


def _seconds(value: float) -> str:
    """`value` seconds, in a form that fits a column.

    Fixed-point normally, exponential once fixed-point would outgrow the
    cell. The bound is on *width*, not on meaning: there is no period this
    refuses to state, only a point past which it states it compactly.
    """
    plain = f"{value:,.1f}"
    return plain if len(plain) <= MAX_SECONDS_WIDTH else f"{value:.1e}"


class RowKind(enum.Enum):
    """Which population a planned row belongs to.

    The renderer files `TASK` under one `Progress` and the other two under
    another, which is what keeps exact bars above inferred ones when a frame
    is cropped.
    """

    TASK = "task"
    LOOP = "loop"
    POSITION = "position"


#: What a renderer files its handle under. A loop row and the position row
#: beneath it share a `SourceKey` — the position row has no identity of its
#: own — so the kind is what separates them.
RowKey = tuple[RowKind, SourceKey | int]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class PlanRow:
    """One row the display was told to draw, in the terms a renderer receives.

    Every string is already formed: `label` carries its indent and a position
    row's padding, and the three cell fields are final text. A renderer that
    reformatted any of them would be deciding, which is the thing this type
    exists to stop it doing.
    """

    kind: RowKind
    key: RowKey
    #: Indented by depth, and padded to the column width for a position row.
    label: str
    #: None means *pulse* — no claim. This is the withdrawal channel, and the
    #: reason a renderer cannot use `Progress.update(total=...)` to apply it.
    total: int | None
    completed: int
    #: The count cell: "3/20 · 400 iterations", "3 of 5", or "" for a task.
    detail: str
    #: "121/s", "1.5s each", "idle", or "" — always "" on a task row.
    rate: str
    #: "7/100", "7", or "" — always "" on a loop or position row.
    count: str
    collapsed: bool
    subrow: bool
    #: Freeze the elapsed clock. Only an `end` event sets this: it is the one
    #: exact completion signal in the display, and idleness is not one.
    done: bool
    #: Kept beside the indent already baked into `label`, so a test can assert
    #: the depth without counting spaces.
    depth: int


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class HeartbeatLine:
    """The session row, already resolved against the output encoding."""

    glyph: str
    #: Count and rate, padded to the summary column's width.
    summary: str
    message: str | None


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class FrameCounts:
    """ "How many bars" is not one number. It is these seven.

    `sources` is the **identity layer** — call sites, counting records, the
    number the store corroborates and the exit summary prints. `loops` is the
    **display layer** — inferred loops, counting iterations. Taking the first
    for the second is what CLAUDE.md calls the root mistake, so they are
    separate fields and there is deliberately no attribute named `bars`.
    """

    #: Every source location the model has folded into a loop row, **including
    #: rows the ceiling kept off screen**. A ceiling is a display bound, and
    #: this is not a display number: it says what was captured, which is the
    #: question the store answers and the one the exit report asks. Scoping it
    #: to the drawn rows made it quietly disagree with the sentence above.
    sources: int
    #: Inferred loops the model produced, before any ceiling.
    loops: int
    #: ...and after it.
    drawn_loops: int
    suppressed_loops: int
    #: Position sub-rows. Not capped: a position row rides along with the loop
    #: that earned it, so the ceiling counts loops rather than lines.
    positions: int
    #: Named bars from `task()` and `track()`.
    tasks: int
    #: 0 or 1. The existence gate, not the state — a row reading "0 events" is
    #: true and worth nothing, so the heartbeat appears only once something
    #: has arrived.
    heartbeat: int


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """Everything one redraw was told to draw, top to bottom."""

    heartbeat: HeartbeatLine | None
    tasks: tuple[PlanRow, ...]
    #: Loop and position rows **interleaved**, in screen order: a position row
    #: follows the loop it belongs to. The renderer's re-layout wants exactly
    #: this sequence, because a position row has no place in the containment
    #: order and nothing downstream could work out where it goes.
    rows: tuple[PlanRow, ...]
    counts: FrameCounts

    def of(self, label: str) -> PlanRow | None:
        """The first row whose label contains `label`, ignoring the indent.

        A convenience for assertions, which want to name a row the way a
        reader would rather than reconstruct its padding.
        """
        for row in (*self.tasks, *self.rows):
            if label in row.label:
                return row
        return None


def format_rate(row: LoopRow) -> str:
    """How fast a loop is iterating, or what stopped it.

    Rate rather than period because "12/s" is what a reader wants from a
    loop, and sub-1/s loops are the ones where the period is the readable
    form instead. Blank until two records have been seen: one record
    establishes no interval, and a made-up number is worse than none.

    "idle" rather than "done", because idleness is what was measured — no log
    line announces the end of a loop, so a silence long enough to retire the
    bar is the whole of the evidence.
    """
    if row.idle:
        return "idle"
    if row.rate is None:
        return ""
    if row.rate >= 1:
        return f"{row.rate:,.0f}/s"
    return f"{_seconds(1 / row.rate)}s each"


def format_source_detail(row: LoopRow, separator: str) -> str:
    """The count column: iterations always, cycle position when inferred.

    **Iterations, not records.** `siblings` reads 400 and not the 1600 log
    calls behind it — every call site there says `row %d`, so the row is the
    unit of work and how many lines narrate each one is the author's choice.
    Record count is an identity-layer number, and the store and the exit
    summary are where it is asked for.

    Both numbers earn their place. The iteration count is the one thing here
    that is certainly true; the cycle position is the inferred part and is what
    the bar's fill is showing, so a reader can see the guess beside the fact.
    """
    iterations = f"{row.count:,} iterations"
    if not row.is_determinate:
        return iterations
    return f"{row.cycle_current}/{row.total} {separator} {iterations}"


def format_position_detail(position: CyclePosition) -> str:
    """The count column for a position row: which stage of how many.

    "of" rather than the loop row's slash, because the two numbers mean
    different things and should not look alike. `3/5` on a loop row is an
    inferred cycle position — a guess. `3 of 5` here is the AST's count of the
    call sites in a body and the ordinal of the one that just fired, which is
    read rather than measured.
    """
    return f"{position.current} of {position.total}"


def format_heartbeat_summary(state: HeartbeatState, separator: str) -> str:
    """The heartbeat's count-and-rate cell, padded to hold its column.

    The rate carries one decimal where a source bar's carries none. A loop at
    121/s does not need the tenth, but a session ticking over at 2.4/s does:
    rounding that to "2/s" throws away the difference between a program
    creeping along and one that has nearly stopped.

    Padded to `HEARTBEAT_SUMMARY_WIDTH`, which is a floor rather than a
    ceiling — a session busy enough to outgrow it pushes the message right
    rather than losing any of it.
    """
    plural = "" if state.events == 1 else "s"
    summary = f"{state.events:,} event{plural}"
    if state.rate is not None:
        summary += (
            f" {separator} {state.rate:,.1f}/s"
            if state.rate >= 1
            else f" {separator} {_seconds(1 / state.rate)}s each"
        )
    return f"{summary:<{HEARTBEAT_SUMMARY_WIDTH}}"


def _loop_row(row: LoopRow, separator: str) -> PlanRow:
    """One inferred loop: pulsing, determinate, or collapsed.

    Three states, and which one applies is entirely the model's call:

    * **Pulsing** (`total=None`) while nothing bounds the loop. That is the
      permanent state of an outermost loop — nothing encloses it, so nothing
      says how long it is — and the starting state of every other one until
      containment analysis has held an answer for two polls.
    * **Determinate** once an enclosing loop gives the cycle a length. The
      fill shows position *within the current cycle*, so a nested bar fills,
      resets and fills again, while the count column keeps the cumulative
      iteration count.
    * **Collapsed**, when the loop has been quiet for long enough to be
      presumed over. The row is filled and marked: idleness is the only
      completion signal this data has, so acting on it is the claim being
      made, and the rate column says "idle" rather than a stale rate so the
      claim is legible rather than implied. The row stays where it is —
      deleting it would empty the final frame.
    """
    if row.idle:
        total: int | None = row.count
        completed = row.count
    elif row.is_determinate:
        total, completed = row.total, row.cycle_current
    else:
        total, completed = None, row.count
    return PlanRow(
        kind=RowKind.LOOP,
        key=(RowKind.LOOP, row.key),
        label=f"{'  ' * row.depth}{row.label}",
        total=total,
        completed=completed,
        detail=format_source_detail(row, separator),
        rate=format_rate(row),
        count="",
        collapsed=row.idle,
        subrow=False,
        done=False,
        depth=row.depth,
    )


def _position_row(row: LoopRow, position: CyclePosition) -> PlanRow:
    """The second row: how far through its body this iteration has got.

    Determinate always, and that is the difference between it and every other
    inferred row. The loop row pulses because nothing bounds a loop; this one
    is bounded by construction — the body has as many call sites as the source
    says it has, and the ordinal of the one that just fired is read from the
    same place. There is no ratio, no confirmation count and nothing to
    withdraw, so it starts full-width and stays that way.

    Padded to the widest stage this body can ever show, so the column holds
    still: a grid column is as wide as its widest cell, and a label that
    changed length five times an iteration would slide every bar sideways.

    Collapses with the loop above it. A finished loop's last stage is worth
    keeping on screen — it says where the work stopped — but it should stop
    shouting, exactly as the bar above it does.
    """
    return PlanRow(
        kind=RowKind.POSITION,
        key=(RowKind.POSITION, row.key),
        label=f"{'  ' * (row.depth + 1)}{position.label:<{position.width}}",
        total=position.total,
        completed=position.current,
        detail=format_position_detail(position),
        rate="",
        count="",
        collapsed=row.idle,
        subrow=True,
        done=False,
        depth=row.depth + 1,
    )


def _task_row(bar: TaskBarState) -> PlanRow:
    """What `task()` and `track()` reported. No inference.

    `total=None` gives an indeterminate, pulsing bar, which is the honest
    rendering of a task that never said how much work there was. A task that
    did say gets a real percentage.

    A task that *overshoots* its total goes back to pulsing. The count column
    keeps showing the real numbers, so the overshoot is visible rather than
    merely implied.

    A task that *ends* is filled, whatever it claimed on the way. Its `end`
    row is an exact completion signal — the one kind of bar here that has one
    — so leaving an indeterminate task pulsing after it would say "still
    working" about work that is provably over, and would leave the elapsed
    clock running with it.
    """
    if bar.total is not None:
        count = f"{bar.current}/{bar.total}"
    elif bar.current:
        count = str(bar.current)
    else:
        # A task that never reported progress — usually a container for
        # subtasks. A bare "0" beside a pulsing bar reads as "stuck at zero"
        # rather than "no count was claimed".
        count = ""
    completed: int = bar.current
    drawn_total: int | None
    if bar.done and bar.total is None:
        # Nothing was ever claimed, so completion can only be expressed as
        # "all of whatever it did". Floored at 1 because a container task —
        # one that only held subtasks and reported no count of its own — is
        # 0 of 0, which rich renders as a full bar labelled 0% and which
        # reads as a failure.
        drawn_total = max(bar.current, 1)
        completed = drawn_total
    elif bar.total is not None and bar.current > bar.total:
        drawn_total = None
    else:
        # A task that ended *short* of a total it claimed keeps both numbers:
        # stopping at 5/10 is a fact, and filling the bar would overwrite it
        # with a claim of 10.
        drawn_total = bar.total
    return PlanRow(
        kind=RowKind.TASK,
        key=(RowKind.TASK, bar.task_id),
        label=f"{'  ' * bar.depth}{bar.label}",
        total=drawn_total,
        completed=completed,
        detail="",
        rate="",
        count=count,
        collapsed=False,
        subrow=False,
        done=bar.done,
        depth=bar.depth,
    )


def plan_frame(
    *,
    rows: Sequence[LoopRow],
    tasks: Sequence[TaskBarState],
    heartbeat: HeartbeatState,
    frames: str,
    separator: str,
    max_bars: int | None = None,
) -> Frame:
    """Decide everything one frame draws, from what `poll()` already returned.

    **Takes polled values, never models, and that is load-bearing.**
    `RepeatingSourceModel.bars()` recomputes idleness against its clock on
    every call, and `LoopRowModel.rows()` is a *mutating* read — it reaches
    `CyclePositionModel`, whose admission is one-way. Taking models would let
    this re-ask and describe a frame that was never on screen. Taking values
    makes that impossible by construction rather than by discipline, and it is
    what lets a test fabricate a `LoopRow` directly instead of rebuilding a
    multi-poll timing setup to reach one branch.

    The ceiling truncates *loops*, not lines: a position row rides along with
    the loop that earned it. Truncating rather than hiding is deliberate —
    `add_task()` refreshes the display on every call, so registering rows we
    will never draw costs a redraw each.
    """
    loops = list(rows)
    drawn = loops if max_bars is None else loops[:max_bars]
    planned: list[PlanRow] = []
    positions = 0
    for row in drawn:
        planned.append(_loop_row(row, separator))
        if row.position is not None:
            planned.append(_position_row(row, row.position))
            positions += 1
    task_rows = tuple(_task_row(bar) for bar in tasks)
    line = (
        HeartbeatLine(
            glyph=heartbeat.glyph(frames),
            summary=format_heartbeat_summary(heartbeat, separator),
            message=heartbeat.message or None,
        )
        if heartbeat.events
        else None
    )
    return Frame(
        heartbeat=line,
        tasks=task_rows,
        rows=tuple(planned),
        counts=FrameCounts(
            # Every qualified source is appended to exactly one row's members,
            # so summing them is the identity layer without re-asking `bars()`
            # — which would recompute idleness against a second clock reading.
            # Over `loops` and not `drawn`: the ceiling bounds what is drawn,
            # never what was captured.
            sources=sum(len(row.members) for row in loops),
            loops=len(loops),
            drawn_loops=len(drawn),
            suppressed_loops=len(loops) - len(drawn),
            positions=positions,
            tasks=len(task_rows),
            heartbeat=1 if line is not None else 0,
        ),
    )
