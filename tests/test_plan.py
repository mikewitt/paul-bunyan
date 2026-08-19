"""What the display decided, asserted without drawing anything.

`plan_frame()` imports no `rich`, so every branch of the display's policy is
reachable on the bare install — where, until this file, none of it was
exercised at all. That is the point of the split and the reason this file
exists beside `test_display_parity.py` rather than instead of it. Three
layers, and each catches what the others cannot:

* **here** — the decision is right;
* `test_display_parity.py` — rich holds exactly what was decided;
* the frame-text assertions in `test_render_progress.py` — rich draws it.

A wrong decision is invisible to parity, because the painter faithfully
applies whatever it is handed. Measured on the commit before the extraction:
filling a determinate row from `row.count` instead of `row.cycle_current`
passed the whole 606-test suite, and it still passes parity today, because
plan and paint agree on the wrong number. It fails here.

**Rows are fabricated, not driven.** `plan_frame()` takes polled values rather
than models precisely so a branch can be reached by constructing the value
that reaches it, instead of by rebuilding a multi-poll timing setup that
already has coverage in `test_progress_loops.py`. Timing is that file's
subject; this file's subject is what is done with the answer.
"""

from __future__ import annotations

import pytest

from lumberjack.renderers.plan import (
    Frame,
    RowKind,
    plan_frame,
)
from lumberjack.renderers.progress import (
    CyclePosition,
    HeartbeatState,
    LoopRow,
    TaskBarState,
)
from lumberjack.schema import SourceKey

#: Tier 2 — a component contract, driven through a public component API.
#: See tests/README.md; `test_tier2_rules.py` checks what the mark claims.
pytestmark = pytest.mark.tier2

SEP = "·"
WIDTH = 26


def key(lineno: int = 1) -> SourceKey:
    return SourceKey(pathname="/x.py", lineno=lineno, func_name="run")


def loop(**overrides: object) -> LoopRow:
    """A `LoopRow` with the fields a plan reads, overridable one at a time."""
    fields: dict[str, object] = {
        "key": key(),
        "members": (key(),),
        "label": "parsed …",
        "clock": key(),
        "count": 40,
    }
    fields.update(overrides)
    return LoopRow(**fields)  # type: ignore[arg-type]


def task(**overrides: object) -> TaskBarState:
    fields: dict[str, object] = {
        "task_id": 1,
        "label": "job",
        "current": 0,
        "total": None,
        "done": False,
        "depth": 0,
    }
    fields.update(overrides)
    return TaskBarState(**fields)  # type: ignore[arg-type]


def stage(current: int, total: int, label: str, width: int) -> CyclePosition:
    """A `CyclePosition`; `source` names the call site and nothing here reads
    it, so it is filled in rather than parametrized."""
    return CyclePosition(
        current=current, total=total, label=label, source=key(), width=width
    )


def frame(
    rows: list[LoopRow] | None = None,
    tasks: list[TaskBarState] | None = None,
    heartbeat: HeartbeatState | None = None,
    **kwargs: object,
) -> Frame:
    return plan_frame(
        rows=rows or [],
        tasks=tasks or [],
        heartbeat=heartbeat if heartbeat is not None else HeartbeatState(),
        frames="⠋⠙",
        separator=SEP,
        summary_width=WIDTH,
        **kwargs,  # type: ignore[arg-type]
    )


# --- the three states of a loop row -----------------------------------------


def test_a_loop_nothing_bounds_pulses():
    """`total=None` is the no-claim channel, and the permanent state of an
    outermost loop: nothing encloses it, so nothing says how long it is."""
    (row,) = frame([loop()]).rows
    assert row.total is None
    assert row.completed == 40
    assert row.detail == "40 iterations"


def test_a_determinate_row_fills_against_the_cycle_not_the_run():
    """The mutation this exists for. `completed` drives the bar's fill, and
    the fill shows position within the *current* cycle — so a nested bar
    fills, resets and fills again while the count column keeps the cumulative
    total. Filling from `count` instead passed the entire suite before the
    plan existed, and still passes the parity test, because plan and paint
    agree on the wrong number.
    """
    (row,) = frame([loop(count=400, total=20, cycle_current=8)]).rows
    assert row.completed == 8, "the fill is the cycle position"
    assert row.total == 20
    assert row.detail == f"8/20 {SEP} 400 iterations", "the count column keeps the run"


def test_an_idle_row_is_filled_marked_and_says_so():
    """Idleness is the only completion signal this data has, so acting on it
    is the claim being made — and the rate column says "idle" rather than a
    stale rate, so the claim is legible rather than implied."""
    (row,) = frame([loop(idle=True)]).rows
    assert (row.total, row.completed) == (40, 40)
    assert row.collapsed
    assert row.rate == "idle"


def test_an_idle_row_beats_a_total_it_had():
    """A row that had a cycle claim and then went quiet fills to its own
    count, not to the claim: retiring says "this stopped", not "this
    finished 20 of 20"."""
    (row,) = frame([loop(count=400, total=20, cycle_current=8, idle=True)]).rows
    assert (row.total, row.completed) == (400, 400)


# --- the rate cell ----------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (None, ""),
        (1 / 121, "121/s"),
        (1.5, "1.5s each"),
        (1.0, "1/s"),
    ],
)
def test_the_rate_cell_switches_form_at_one_per_second(period, expected):
    """Rate above 1/s because "12/s" is what a reader wants from a loop, and
    period below it because "0.7s each" beats "0/s". Blank until an interval
    has actually been measured: one record establishes none, and a made-up
    number is worse than no number."""
    assert frame([loop(period=period)]).rows[0].rate == expected


# --- the position row -------------------------------------------------------


def test_a_position_row_follows_the_loop_it_belongs_to():
    """Interleaved rather than appended, because the renderer's re-layout
    wants screen order: a position row has no place in the containment order
    and nothing downstream could work out where it goes."""
    position = stage(3, 5, "validating", 12)
    planned = frame([loop(depth=1, position=position)]).rows
    assert [row.kind for row in planned] == [RowKind.LOOP, RowKind.POSITION]
    assert planned[1].subrow
    assert planned[1].depth == 2, "indented one past its loop"
    assert planned[1].detail == "3 of 5"


def test_a_position_row_is_padded_so_the_column_holds_still():
    """A grid column is as wide as its widest cell, and a label that changed
    length five times an iteration would slide every bar sideways with it."""
    position = stage(1, 3, "open", 20)
    planned = frame([loop(position=position)]).rows
    assert planned[1].label == f"  {'open':<20}"


def test_a_position_row_collapses_with_the_loop_above_it():
    """A finished loop's last stage is worth keeping on screen — it says where
    the work stopped — but it should stop shouting."""
    position = stage(2, 3, "fetch", 8)
    planned = frame([loop(idle=True, position=position)]).rows
    assert all(row.collapsed for row in planned)


def test_a_loop_without_a_position_draws_one_row():
    assert len(frame([loop()]).rows) == 1


# --- task bars --------------------------------------------------------------


def test_a_task_that_claimed_nothing_pulses_with_no_count():
    """A bare "0" beside a pulsing bar reads as "stuck at zero" rather than
    "no count was claimed"."""
    (row,) = frame(tasks=[task()]).tasks
    assert row.total is None
    assert row.count == ""


def test_a_task_that_overshoots_withdraws_its_claim():
    """Back to pulsing rather than showing 250%. The count column keeps the
    real numbers, so the overshoot is visible rather than merely implied."""
    (row,) = frame(tasks=[task(current=25, total=10)]).tasks
    assert row.total is None
    assert row.count == "25/10"


def test_a_task_that_ended_short_keeps_both_numbers():
    """Stopping at 5/10 is a fact, and filling the bar would overwrite it with
    a claim of 10."""
    (row,) = frame(tasks=[task(current=5, total=10, done=True)]).tasks
    assert (row.total, row.completed) == (10, 5)
    assert row.done


def test_a_container_task_that_ends_is_full_rather_than_zero_of_zero():
    """0 of 0 renders as a full bar labelled 0%, which reads as a failure."""
    (row,) = frame(tasks=[task(current=0, done=True)]).tasks
    assert (row.total, row.completed) == (1, 1)


def test_a_task_that_ends_having_counted_fills_to_what_it_did():
    """Nothing was ever claimed, so completion can only be expressed as "all
    of whatever it did"."""
    (row,) = frame(tasks=[task(current=7, done=True)]).tasks
    assert (row.total, row.completed) == (7, 7)
    assert row.count == "7"


def test_a_task_row_carries_no_loop_cells():
    """One mapping for both populations, so a column added to one is a column
    the other explicitly blanks — an omitted key reads as None and renders as
    the string "None"."""
    (row,) = frame(tasks=[task(current=3, total=9)]).tasks
    assert (row.rate, row.detail) == ("", "")
    assert frame([loop()]).rows[0].count == ""


# --- the heartbeat ----------------------------------------------------------


def test_no_heartbeat_before_anything_has_arrived():
    """A row reading "0 events" is true and worth nothing: it earns its place
    by carrying a number that moves."""
    planned = frame()
    assert planned.heartbeat is None
    assert planned.counts.heartbeat == 0


def test_the_heartbeat_carries_its_glyph_count_and_rate():
    state = HeartbeatState(events=5, period=1 / 2.4, beat=1, message="hello")
    line = frame(heartbeat=state).heartbeat
    assert line is not None
    assert line.glyph == "⠙", "the beat indexes the frame set the console can take"
    assert line.summary.startswith(f"5 events {SEP} 2.4/s")
    assert len(line.summary) == WIDTH, "padded so the message holds one column"
    assert line.message == "hello"


def test_a_slow_session_is_described_by_its_period():
    """Rounding 0.4/s to "0/s" would throw away the difference between a
    program creeping along and one that has stopped."""
    line = frame(heartbeat=HeartbeatState(events=2, period=2.5)).heartbeat
    assert line is not None
    assert f"2 events {SEP} 2.5s each" in line.summary


def test_one_event_is_not_pluralised():
    line = frame(heartbeat=HeartbeatState(events=1)).heartbeat
    assert line is not None
    assert line.summary.startswith("1 event ")


# --- the counts, which are seven numbers and not one ------------------------


def test_sources_counts_call_sites_and_loops_counts_loops():
    """The identity layer and the display layer, which disagree by design:
    `siblings` is 400 iterations behind 1600 records across 4 call sites, and
    taking the first number for the second is what CLAUDE.md calls the root
    mistake."""
    merged = loop(members=(key(1), key(2), key(3), key(4)), count=400)
    counts = frame([merged]).counts
    assert counts.sources == 4
    assert counts.loops == 1
    assert not hasattr(counts, "bars")


def test_the_ceiling_bounds_what_is_drawn_and_counts_what_it_hid():
    rows = [loop(key=key(i), members=(key(i),), label=f"row {i}") for i in range(5)]
    counts = frame(rows, max_bars=2).counts
    assert (counts.loops, counts.drawn_loops, counts.suppressed_loops) == (5, 2, 3)


def test_the_ceiling_does_not_change_what_was_captured():
    """A ceiling is a *display* bound, and `sources` is not a display number.

    It said 4 here — the two rows drawn — which contradicted its own
    docstring: the identity layer is what the store would corroborate, and
    the store does not stop counting because a terminal ran out of rows.
    `teardown._report_display()` reads it, so the understatement reached the
    one place that survives the terminal scrolling.
    """
    rows = [loop(key=key(i), members=(key(i), key(i + 10))) for i in range(5)]
    counts = frame(rows, max_bars=2).counts
    assert counts.sources == 10
    assert counts.drawn_loops == 2, "the ceiling still bounds what is drawn"


def test_no_ceiling_suppresses_nothing():
    rows = [loop(key=key(i), members=(key(i),)) for i in range(5)]
    counts = frame(rows).counts
    assert (counts.drawn_loops, counts.suppressed_loops) == (5, 0)


def test_a_position_row_rides_along_rather_than_taking_a_slot():
    """The ceiling counts loops, not lines: a position row belongs to the loop
    that earned it, and dropping it would leave that loop half-drawn."""
    position = stage(1, 3, "a", 4)
    rows = [
        loop(key=key(i), members=(key(i),), position=position if i < 2 else None)
        for i in range(5)
    ]
    counts = frame(rows, max_bars=2).counts
    assert counts.drawn_loops == 2
    assert counts.positions == 2
    assert len(frame(rows, max_bars=2).rows) == 4


def test_tasks_are_counted_separately_from_loops():
    counts = frame([loop()], tasks=[task(), task(task_id=2)]).counts
    assert (counts.tasks, counts.loops) == (2, 1)


# --- finding a row ----------------------------------------------------------


def test_a_row_can_be_found_by_the_name_a_reader_would_use():
    """Ignoring the indent, which is an implementation detail of the label."""
    planned = frame([loop(depth=2, label="reconciling batch …")])
    found = planned.of("reconciling batch …")
    assert found is not None
    assert found.depth == 2
    assert planned.of("nothing like this") is None
