"""Tests for the naive repeating-source model behind the live bar.

The premise under test: records from the same source location are one
repeating shape, and the store already groups them that way. No `rich` here —
the model is display-independent, so it runs on a bare install.
"""

from __future__ import annotations

import pytest

from lumberjack.renderers.progress import (
    CONTAINMENT_CONFIRMATIONS,
    DEFAULT_MIN_REPEATS,
    MAX_BARS_ENV_VAR,
    PERIOD_SMOOTHING,
    BarState,
    RepeatingSourceModel,
    TaskProgressModel,
    resolve_max_bars,
)
from lumberjack.schema import SourceKey


def _loop_rows(make_row, n: int, **overrides):
    """N records from one source location, the way a loop emits them."""
    return [make_row(message=f"item {i}", **overrides) for i in range(n)]


def test_default_threshold_is_a_loop_not_a_coincidence():
    assert DEFAULT_MIN_REPEATS >= 2


def test_no_bars_before_the_first_poll(store):
    model = RepeatingSourceModel(store)
    assert model.bars() == []


def test_a_source_below_the_threshold_gets_no_bar(store, make_row):
    store.append(_loop_rows(make_row, 2))
    model = RepeatingSourceModel(store, min_repeats=3)
    assert model.poll() == []


def test_a_repeating_source_gets_a_bar(store, make_row):
    store.append(_loop_rows(make_row, 5))
    model = RepeatingSourceModel(store, min_repeats=3)
    (bar,) = model.poll()
    assert bar.count == 5
    assert bar.source == SourceKey("/tmp/foo.py", 10, "bar")


def test_the_bar_advances_as_the_loop_runs(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 3))
    assert [b.count for b in model.poll()] == [3]
    store.append(_loop_rows(make_row, 7))
    assert [b.count for b in model.poll()] == [10]


def test_bars_reports_the_last_poll(store, make_row):
    store.append(_loop_rows(make_row, 4))
    model = RepeatingSourceModel(store, min_repeats=3)
    assert model.poll() == model.bars()


def test_each_source_location_gets_its_own_bar(store, make_row):
    # Two loops in two places — the multiple-workers case falls out of the
    # same grouping, with no extra machinery.
    store.append(_loop_rows(make_row, 4, lineno=10))
    store.append(_loop_rows(make_row, 6, lineno=99, func_name="other"))
    model = RepeatingSourceModel(store, min_repeats=3)
    assert {(b.source.lineno, b.count) for b in model.poll()} == {(10, 4), (99, 6)}


def test_newly_seen_sources_are_ordered_busiest_first(store, make_row):
    store.append(_loop_rows(make_row, 4, lineno=10))
    store.append(_loop_rows(make_row, 9, lineno=99))
    model = RepeatingSourceModel(store, min_repeats=3)
    assert [b.source.lineno for b in model.poll()] == [99, 10]


def test_a_bar_keeps_its_slot_when_another_overtakes_it(store, make_row):
    # A bar that jumps up and down the screen as counts cross each other is
    # unreadable, so position is first-seen order, not current rank.
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 5, lineno=10))
    assert [b.source.lineno for b in model.poll()] == [10]
    store.append(_loop_rows(make_row, 50, lineno=99))
    assert [b.source.lineno for b in model.poll()] == [10, 99]


def test_a_bar_never_counts_backwards_after_eviction(store, make_row):
    # The store is a window on the last N records; a bar is a count of work
    # done. Trimming the former must not rewrite the latter.
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 10))
    assert [b.count for b in model.poll()] == [10]
    store.evict(keep_last=2)
    assert [b.count for b in model.poll()] == [10]
    store.append(_loop_rows(make_row, 3))
    assert [b.count for b in model.poll()] == [13]


def test_records_are_counted_once_however_often_it_polls(store, make_row):
    # The watermark is the whole mechanism: re-polling without new records
    # must add nothing, or every idle redraw would inflate the bar.
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 4))
    assert [b.count for b in model.poll()] == [4]
    for _ in range(5):
        assert [b.count for b in model.poll()] == [4]


def test_a_source_qualifies_on_its_total_not_one_poll(store, make_row):
    # Records dribbling in below the threshold still accumulate, so a slow
    # loop earns its bar on the poll that takes it over the line.
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(2):
        store.append(_loop_rows(make_row, 1))
        assert model.poll() == []
    store.append(_loop_rows(make_row, 1))
    assert [b.count for b in model.poll()] == [3]


def test_label_names_the_source_location():
    bar = BarState(source=SourceKey("/srv/app/worker.py", 42, "process"), count=1)
    assert bar.label == "worker.py:42 process()"


# --- the opt-in bar ceiling ------------------------------------------------
#
# An environment variable rather than an init() option, and a debug aid rather
# than a feature: see the note on MAX_BARS_ENV_VAR and issue #8.


def test_no_ceiling_by_default(monkeypatch):
    monkeypatch.delenv(MAX_BARS_ENV_VAR, raising=False)
    assert resolve_max_bars() is None


def test_ceiling_from_the_environment(monkeypatch):
    monkeypatch.setenv(MAX_BARS_ENV_VAR, "12")
    assert resolve_max_bars() == 12


def test_an_explicit_ceiling_beats_the_environment(monkeypatch):
    monkeypatch.setenv(MAX_BARS_ENV_VAR, "12")
    assert resolve_max_bars(3) == 3


@pytest.mark.parametrize("value", ["banana", "", "0", "-4", "3.5"])
def test_an_unusable_environment_value_warns_and_draws_everything(monkeypatch, value):
    """An operator typo must not cap at something surprising, or take the run
    down. Same split as LUMBERJACK_OUTPUT_MODE: env typos warn and degrade."""
    monkeypatch.setenv(MAX_BARS_ENV_VAR, value)
    if value == "":
        # Unset and empty are the same request: no ceiling, nothing to warn about.
        assert resolve_max_bars() is None
        return
    with pytest.warns(RuntimeWarning, match=MAX_BARS_ENV_VAR):
        assert resolve_max_bars() is None


@pytest.mark.parametrize("value", [0, -1])
def test_an_unusable_explicit_ceiling_raises(monkeypatch, value):
    """A bad argument is the caller's bug, so it raises rather than warns."""
    monkeypatch.delenv(MAX_BARS_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="must be positive"):
        resolve_max_bars(value)


# --- named bars from the tracking API --------------------------------------
#
# No inference here at all: every number came from a `task()` or `track()`
# call that stated it. This model only reads.


def _event(make_row, task_id, event, **kw):
    return make_row(
        task_id=task_id,
        task_event=event,
        task_label=kw.pop("label", "job"),
        **kw,
    )


def test_no_task_bars_before_the_first_poll(store):
    assert TaskProgressModel(store).bars() == []


def test_a_task_gets_a_bar_on_its_start_row(store, make_row):
    store.append([_event(make_row, 1, "start", label="reindex")])
    (bar,) = TaskProgressModel(store).poll()
    assert (bar.task_id, bar.label, bar.done) == (1, "reindex", False)


def test_the_latest_row_wins_rather_than_accumulating(store, make_row):
    """`progress_current` is absolute, so the newest row for a task is the
    whole truth about it — which is what makes a named bar exact."""
    model = TaskProgressModel(store)
    store.append(
        [
            _event(make_row, 1, "start", progress_current=0, progress_total=100),
            _event(make_row, 1, "update", progress_current=40, progress_total=100),
        ]
    )
    (bar,) = model.poll()
    assert (bar.current, bar.total) == (40, 100)


def test_polling_twice_without_new_rows_changes_nothing(store, make_row):
    model = TaskProgressModel(store)
    store.append([_event(make_row, 1, "update", progress_current=7)])
    assert [b.current for b in model.poll()] == [7]
    assert [b.current for b in model.poll()] == [7]


def test_a_task_without_a_total_is_indeterminate(store, make_row):
    store.append([_event(make_row, 1, "update", progress_current=7)])
    (bar,) = TaskProgressModel(store).poll()
    assert bar.total is None and not bar.is_determinate


def test_a_task_with_a_total_is_determinate(store, make_row):
    store.append([_event(make_row, 1, "update", progress_current=7, progress_total=10)])
    (bar,) = TaskProgressModel(store).poll()
    assert bar.is_determinate


def test_the_end_row_finishes_the_bar(store, make_row):
    """An exact completion signal, unlike a source bar, which has none."""
    model = TaskProgressModel(store)
    store.append([_event(make_row, 1, "start")])
    assert [b.done for b in model.poll()] == [False]
    store.append([_event(make_row, 1, "end", progress_current=9)])
    (bar,) = model.poll()
    assert bar.done and bar.current == 9


def test_depth_comes_from_the_parent_chain(store, make_row):
    store.append(
        [
            _event(make_row, 1, "start", label="root"),
            _event(make_row, 2, "start", label="child", parent_task_id=1),
            _event(make_row, 3, "start", label="grandchild", parent_task_id=2),
        ]
    )
    assert [(b.label, b.depth) for b in TaskProgressModel(store).poll()] == [
        ("root", 0),
        ("child", 1),
        ("grandchild", 2),
    ]


def test_a_broken_parent_chain_does_not_hang(store, make_row):
    """Defensive only against data, not against a bug: a store trimmed by
    `evict()` can leave a child whose parent's rows are gone."""
    store.append([_event(make_row, 2, "start", label="orphan", parent_task_id=99)])
    (bar,) = TaskProgressModel(store).poll()
    assert bar.depth == 0


def test_task_bars_keep_their_slot(store, make_row):
    """Append-only, as source bars are: a bar that moves is unreadable.
    Children land after parents for free, since a parent must exist before
    `subtask()` can be called on it."""
    model = TaskProgressModel(store)
    store.append([_event(make_row, 1, "start", label="first")])
    model.poll()
    store.append(
        [
            _event(make_row, 2, "start", label="second"),
            _event(make_row, 1, "update", label="first", progress_current=99),
        ]
    )
    assert [b.label for b in model.poll()] == ["first", "second"]


# --- how fast is this loop going? -------------------------------------------
#
# The cheap half of #38. A source's own recurrence interval *is* its loop's
# period, so timing needs no clustering — only deciding how many bars to draw
# does. Nothing here infers containment.


def test_no_period_before_two_records(store, make_row):
    """One record establishes no interval, and a made-up number is worse
    than none."""
    store.append([make_row(created=100.0)])
    model = RepeatingSourceModel(store, min_repeats=1)
    (bar,) = model.poll()
    assert bar.period is None and bar.rate is None


def test_the_period_comes_from_the_deltas_own_span_on_first_sight(store, make_row):
    """Three records a second apart: two intervals, so one second each."""
    store.append([make_row(created=100.0 + i) for i in range(3)])
    model = RepeatingSourceModel(store, min_repeats=1)
    (bar,) = model.poll()
    assert bar.period == pytest.approx(1.0)
    assert bar.rate == pytest.approx(1.0)


def test_the_period_spans_the_gap_between_polls(store, make_row):
    """The window runs from the last record we already knew about, not from
    the delta's own first record — otherwise the quiet time between polls is
    invisible and every loop looks faster than it is."""
    model = RepeatingSourceModel(store, min_repeats=1)
    store.append([make_row(created=100.0), make_row(created=101.0)])
    model.poll()
    # One record, ten seconds after the last — a ten-second interval, even
    # though this delta spans no time of its own.
    store.append([make_row(created=111.0)])
    (bar,) = model.poll()
    assert bar.period > 1.0, "the between-poll gap was ignored"


def test_the_period_is_smoothed_rather_than_replaced(store, make_row):
    """A single slow iteration should not make the displayed rate lurch."""
    model = RepeatingSourceModel(store, min_repeats=1)
    store.append([make_row(created=100.0 + i) for i in range(5)])
    model.poll()
    steady = model.bars()[0].period
    assert steady == pytest.approx(1.0)

    # One record 96 seconds after the last: an outlier sample of 96s/interval.
    store.append([make_row(created=200.0)])
    (jolted,) = (b.period for b in model.poll())
    blended = PERIOD_SMOOTHING * 96.0 + (1 - PERIOD_SMOOTHING) * 1.0
    assert jolted == pytest.approx(blended)
    assert jolted < 96.0, "the outlier replaced the estimate instead of moving it"


def test_records_sharing_a_timestamp_report_no_rate(store, make_row):
    """A burst inside one clock tick has no interval to learn from, and
    dividing by the span would report an infinite rate."""
    store.append([make_row(created=100.0) for _ in range(5)])
    model = RepeatingSourceModel(store, min_repeats=1)
    (bar,) = model.poll()
    assert bar.period is None and bar.rate is None


def test_each_source_times_itself(store, make_row):
    store.append([make_row(lineno=10, created=100.0 + i) for i in range(3)])
    store.append([make_row(lineno=99, created=100.0 + i * 10) for i in range(3)])
    model = RepeatingSourceModel(store, min_repeats=1)
    periods = {b.source.lineno: b.period for b in model.poll()}
    assert periods[10] == pytest.approx(1.0)
    assert periods[99] == pytest.approx(10.0)


# --- containment: which loop runs inside which ------------------------------


def _nested_rows(make_row, outer_period, inner_period, *, cycles, start=100.0):
    """An outer loop logging on line 4 with an inner loop logging on line 6.

    Emitted in real interleaved order — line 4, then its body's line-6
    records, then line 4 again — because that is the stream the model has to
    read structure out of.
    """
    rows = []
    at = start
    per_cycle = round(outer_period / inner_period)
    for _ in range(cycles):
        rows.append(make_row(lineno=4, func_name="outer", created=at))
        for i in range(per_cycle):
            rows.append(
                make_row(lineno=6, func_name="inner", created=at + i * inner_period)
            )
        at += outer_period
    return rows


def _by_line(bars):
    return {b.source.lineno: b for b in bars}


#: Enough polls for the outer line to reach `min_repeats` and then for the
#: pairing to be confirmed. Confirmations only count polls that brought new
#: records, so this is a number of *cycles*, not a number of redraws.
SETTLED_CYCLES = DEFAULT_MIN_REPEATS + CONTAINMENT_CONFIRMATIONS


def _drive_nested(
    store,
    model,
    make_row,
    *,
    cycles=SETTLED_CYCLES,
    start=100.0,
    outer_period=8.0,
    inner_period=1.0,
):
    """Run a nested loop one enclosing iteration per poll.

    Appending the whole run and then polling repeatedly would be a different
    thing entirely: the later polls carry no records, and a poll with no
    records re-measures nothing, so nothing may be confirmed by one.
    """
    at = start
    bars = {}
    for _ in range(cycles):
        store.append(
            _nested_rows(make_row, outer_period, inner_period, cycles=1, start=at)
        )
        at += outer_period
        bars = _by_line(model.poll())
    return bars, at


def test_a_faster_source_is_inferred_to_run_inside_a_slower_one(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, _ = _drive_nested(store, model, make_row)
    assert bars[6].parent == bars[4].source
    assert bars[4].parent is None, "the outermost loop is enclosed by nothing"


def test_the_ratio_between_the_rates_is_the_inner_loops_total(store, make_row):
    """Containment and the total come from one measurement: line 6 fires
    eight times between consecutive firings of line 4, so eight is both the
    evidence of nesting and the length of the inner loop."""
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, _ = _drive_nested(store, model, make_row)
    assert bars[6].total == 8
    assert bars[4].total is None, "nothing bounds an outermost loop"


def test_the_inner_bar_is_indented_under_its_parent(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, _ = _drive_nested(store, model, make_row)
    assert (bars[4].depth, bars[6].depth) == (0, 1)


def test_three_nested_loops_give_three_depths(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    for cycle in range(SETTLED_CYCLES):
        rows = []
        for step in range(64):
            i = cycle * 64 + step
            if step == 0:
                rows.append(make_row(lineno=4, created=100.0 + i))
            if i % 8 == 0:
                rows.append(make_row(lineno=6, created=100.0 + i))
            rows.append(make_row(lineno=8, created=100.0 + i))
        store.append(rows)
        bars = _by_line(model.poll())
    assert [bars[n].depth for n in (4, 6, 8)] == [0, 1, 2]
    assert bars[8].parent == bars[6].source, "depth 2 must hang off depth 1"


def test_two_lines_in_one_loop_body_are_not_nested(store, make_row):
    """The 1:1 case. Line 6 and line 12 fire once each per iteration, so
    neither runs inside the other — reading a 1:1 ratio as containment would
    invent a one-iteration inner loop for every second log line in a body."""
    model = RepeatingSourceModel(store, min_repeats=3)
    for i in range(SETTLED_CYCLES + 2):
        store.append(
            [
                make_row(lineno=6, created=100.0 + i),
                make_row(lineno=12, created=100.3 + i),
            ]
        )
        bars = _by_line(model.poll())
    assert bars[6].parent is None and bars[12].parent is None
    assert bars[6].total is None and bars[12].total is None


def test_a_ratio_below_the_nesting_floor_is_not_containment(store, make_row):
    """A source 1.5× faster than another cannot be a loop inside it: the
    "inner" loop would run once and a half per outer iteration, which is
    jitter, not structure."""
    model = RepeatingSourceModel(store, min_repeats=3)
    at = 100.0
    for _ in range(SETTLED_CYCLES + 2):
        store.append(
            [make_row(lineno=6, created=at + i * 1.5) for i in range(2)]
            + [make_row(lineno=12, created=at + i) for i in range(3)]
        )
        at += 3.0
        bars = _by_line(model.poll())
    assert bars[6].period == pytest.approx(1.5, rel=0.1)
    assert bars[12].period == pytest.approx(1.0, rel=0.1)
    assert bars[12].parent is None


def test_containment_is_not_believed_on_first_sight(store, make_row):
    """One poll can catch a loop spinning up, where a period built from three
    records means very little. The pairing has to hold before it is drawn."""
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, _ = _drive_nested(store, model, make_row, cycles=DEFAULT_MIN_REPEATS)
    assert bars[6].parent is None, "one observation was enough, and should not be"


def test_a_believed_pairing_is_never_revised(store, make_row):
    """Promotion is one-way, for the same reason bars never move: a bar that
    re-parents or re-scales as the estimate wobbles is unreadable.

    The inner loop speeds up fourfold and holds there, so the ratio would
    re-measure cleanly at 32 and stay long enough to be believed all over
    again — the strongest case for revising, and still refused.
    """
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, at = _drive_nested(store, model, make_row)
    assert bars[6].total == 8, "the pairing under test was never believed"

    for _ in range(SETTLED_CYCLES + 2):
        store.append(_nested_rows(make_row, 8.0, 0.25, cycles=1, start=at))
        at += 8.0
        bars = _by_line(model.poll())
    assert bars[6].total == 8, "the frozen total moved"
    assert bars[6].parent == bars[4].source


def test_an_unstable_ratio_is_never_believed(store, make_row):
    """Two loops whose rates hold no steady relationship are not nested, and
    the only thing separating that from real containment is that a real one
    measures the same way twice."""
    model = RepeatingSourceModel(store, min_repeats=3)
    at = 100.0
    # The outer line keeps a steady 8s; the inner's pace lurches every poll,
    # so the ratio between them never repeats.
    for inner_period in (1.0, 0.1, 2.0, 0.05, 1.5, 0.2):
        rows = [make_row(lineno=4, func_name="outer", created=at)]
        rows += [
            make_row(lineno=6, func_name="inner", created=at + i * inner_period)
            for i in range(round(8.0 / inner_period))
        ]
        store.append(rows)
        at += 8.0
        bars = _by_line(model.poll())
    assert bars[6].parent is None, "an unstable ratio was read as containment"
    assert bars[6].total is None


def test_the_cycle_resets_when_the_enclosing_loop_iterates(store, make_row):
    """A nested bar fills, resets, and fills again — the fill shows position
    within one enclosing iteration, not the run."""
    model = RepeatingSourceModel(store, min_repeats=3)
    _, at = _drive_nested(store, model, make_row)
    store.append(
        [make_row(lineno=4, func_name="outer", created=at)]
        + [make_row(lineno=6, func_name="inner", created=at + i) for i in range(3)]
    )
    bars = _by_line(model.poll())
    assert bars[6].cycle_current == 3, "the cycle counted the whole run"
    assert bars[6].count > 3, "the cumulative count must stay cumulative"


def test_the_cumulative_count_never_resets(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, at = _drive_nested(store, model, make_row)
    before = bars[6].count
    store.append([make_row(lineno=4, func_name="outer", created=at)])
    assert _by_line(model.poll())[6].count == before


def test_an_inner_loop_that_outruns_its_total_stops_claiming_one(store, make_row):
    """Degrading to a pulse is honest; rich would clamp 12/8 to a full bar,
    which reads as finished while the loop is still running."""
    model = RepeatingSourceModel(store, min_repeats=3)
    _, at = _drive_nested(store, model, make_row)
    store.append(
        [make_row(lineno=4, func_name="outer", created=at)]
        + [make_row(lineno=6, func_name="inner", created=at + i) for i in range(30)]
    )
    bar = _by_line(model.poll())[6]
    assert bar.total == 8 and bar.cycle_current > 8
    assert not bar.is_determinate, "an overrun bar must withdraw its claim"


def test_an_untimed_source_takes_no_part_in_containment(store, make_row):
    """Every record in one clock tick, so there is no period and no ratio to
    build a claim on."""
    store.append([make_row(lineno=6, created=100.0) for _ in range(10)])
    store.append([make_row(lineno=12, created=100.0 + i) for i in range(10)])
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(CONTAINMENT_CONFIRMATIONS + 1):
        bars = _by_line(model.poll())
    assert bars[6].parent is None and bars[12].parent is None


# --- retirement: a loop that went quiet -------------------------------------


def _at(t: float):
    return lambda: t


def test_a_source_still_logging_is_not_idle(store, make_row):
    store.append([make_row(created=100.0 + i) for i in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3, clock=_at(105.0))
    (bar,) = model.poll()
    assert not bar.idle


def test_a_source_quiet_for_ten_periods_retires(store, make_row):
    """There is no completion signal — nothing raises `StopIteration` at a log
    line — so a long enough silence is the whole of the evidence."""
    store.append([make_row(created=100.0 + i) for i in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3, clock=_at(104.0 + 11.0))
    (bar,) = model.poll()
    assert bar.idle


def test_a_source_quiet_for_nine_periods_is_merely_slow(store, make_row):
    store.append([make_row(created=100.0 + i) for i in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3, clock=_at(104.0 + 9.0))
    (bar,) = model.poll()
    assert not bar.idle


def test_a_fast_loop_is_not_retired_by_pipeline_latency(store, make_row):
    """A loop iterating every millisecond has a ten-period threshold of 10ms,
    which is shorter than the buffer flush plus redraw interval that feeds
    this model. Without the floor it would retire between frames."""
    store.append([make_row(created=100.0 + i * 0.001) for i in range(20)])
    last = 100.0 + 19 * 0.001
    model = RepeatingSourceModel(store, min_repeats=3, clock=_at(last + 0.5))
    (bar,) = model.poll()
    assert not bar.idle
    assert bar.period == pytest.approx(0.001)


def test_an_untimed_source_is_never_retired(store, make_row):
    """Three records in one clock tick establish no period, and so no scale to
    judge a silence against."""
    store.append([make_row(created=100.0) for _ in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3, clock=_at(1e9))
    (bar,) = model.poll()
    assert bar.period is None and not bar.idle


def test_a_retired_source_that_logs_again_comes_back(store, make_row):
    """Idleness is a statement about now, not a verdict on the run — unlike
    the pulse→determinate promotion, which is one-way."""
    now = 200.0
    store.append([make_row(created=100.0 + i) for i in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3, clock=lambda: now)
    assert model.poll()[0].idle
    now = 300.0
    store.append([make_row(created=299.5)])
    assert not model.poll()[0].idle


def test_a_sibling_line_in_the_same_body_gets_the_same_parent(store, make_row):
    """Two log lines in one nested body iterate together but never measure
    *exactly* together. Without a tolerance the second lands in a level of its
    own, one rung below its twin, where the ratio to that twin is far too
    close to 1 to read as nesting — so it would end up parented by nothing
    while its twin sat correctly under the outer loop."""
    model = RepeatingSourceModel(store, min_repeats=3)
    for cycle in range(SETTLED_CYCLES):
        at = 100.0 + cycle * 8.0
        rows = [make_row(lineno=4, func_name="outer", created=at)]
        for i in range(8):
            rows.append(make_row(lineno=6, func_name="inner", created=at + i))
            rows.append(make_row(lineno=12, func_name="inner", created=at + i * 1.07))
        store.append(rows)
        bars = _by_line(model.poll())
    outer = bars[4].source
    assert bars[6].parent == outer and bars[12].parent == outer
    assert bars[6].depth == bars[12].depth == 1


def test_a_lone_record_before_the_boundary_belongs_to_the_old_cycle(store, make_row):
    """One record in a poll window has no span of its own to apportion across
    the cycle boundary, so the fallback has to know which side it fell on. A
    record that arrived *before* the enclosing loop's newest one is the tail of
    the cycle that just ended, not the start of the next."""
    model = RepeatingSourceModel(store, min_repeats=3)
    _, at = _drive_nested(store, model, make_row)
    store.append(
        [
            make_row(lineno=6, func_name="inner", created=at),
            make_row(lineno=4, func_name="outer", created=at + 5.0),
        ]
    )
    assert _by_line(model.poll())[6].cycle_current == 0


# --- containment is scoped to one worker ------------------------------------


def test_two_loops_on_two_threads_are_never_nested(store, make_row):
    """A loop cannot contain a loop running on another thread, however neatly
    their rates divide. Without this check three independent workers at 4ms,
    10ms and 100ms read as a three-deep hierarchy."""
    at = 100.0
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(CONTAINMENT_CONFIRMATIONS + 2):
        rows = [make_row(lineno=4, thread=1, created=at)]
        rows += [make_row(lineno=6, thread=2, created=at + i) for i in range(8)]
        store.append(rows)
        at += 8.0
        bars = _by_line(model.poll())
    assert bars[6].parent is None, "a loop on another thread was read as the parent"


def test_two_loops_in_one_thread_still_nest(store, make_row):
    """The mirror of the test above, sharing everything but the thread."""
    at = 100.0
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(CONTAINMENT_CONFIRMATIONS + 2):
        rows = [make_row(lineno=4, thread=1, created=at)]
        rows += [make_row(lineno=6, thread=1, created=at + i) for i in range(8)]
        store.append(rows)
        at += 8.0
        bars = _by_line(model.poll())
    assert bars[6].parent == bars[4].source and bars[6].total == 8


def test_two_asyncio_tasks_on_one_thread_are_not_nested(store, make_row):
    """One event loop runs many tasks on one thread, so the thread id alone
    would call two independent coroutines a hierarchy."""
    at = 100.0
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(CONTAINMENT_CONFIRMATIONS + 2):
        rows = [make_row(lineno=4, asyncio_task_id=1, created=at)]
        rows += [
            make_row(lineno=6, asyncio_task_id=2, created=at + i) for i in range(8)
        ]
        store.append(rows)
        at += 8.0
        bars = _by_line(model.poll())
    assert bars[6].parent is None


def test_an_unrelated_worker_between_them_does_not_hide_the_real_parent(
    store, make_row
):
    """Ordering by period alone drops another thread's loop between a real
    parent and its child, so the search has to keep going outward rather than
    give up on the level immediately above."""
    at = 100.0
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(CONTAINMENT_CONFIRMATIONS + 2):
        rows = [make_row(lineno=4, thread=1, created=at)]  # outer, 8s
        rows += [make_row(lineno=5, thread=2, created=at + i * 2) for i in range(4)]
        rows += [make_row(lineno=6, thread=1, created=at + i) for i in range(8)]
        store.append(rows)
        at += 8.0
        bars = _by_line(model.poll())
    assert bars[6].parent == bars[4].source, "the intruder on thread 2 hid the parent"
    assert bars[6].total == 8
    assert bars[5].parent is None


def test_a_poll_that_brought_nothing_confirms_nothing(store, make_row):
    """Periods move only when records do, so a re-read of the same two
    numbers is not a second opinion. Counting every poll would make the
    confirmation a redraw-interval timer wearing the costume of one."""
    model = RepeatingSourceModel(store, min_repeats=3)
    bars, _ = _drive_nested(store, model, make_row, cycles=DEFAULT_MIN_REPEATS)
    assert bars[6].parent is None, "believed before any confirmation was possible"
    for _ in range(20):
        bars = _by_line(model.poll())
    assert bars[6].parent is None, "empty polls confirmed the pairing"


def test_a_candidate_survives_a_quiet_spell(store, make_row):
    """A poll with no records is not evidence *against* a pairing either, so
    the candidate is left standing rather than reset — otherwise a loop slower
    than the redraw interval could never accumulate a confirmation at all."""
    model = RepeatingSourceModel(store, min_repeats=3)
    _, at = _drive_nested(store, model, make_row, cycles=SETTLED_CYCLES - 1)
    for _ in range(5):
        model.poll()
    store.append(_nested_rows(make_row, 8.0, 1.0, cycles=1, start=at))
    assert _by_line(model.poll())[6].total == 8
