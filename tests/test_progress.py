"""Tests for the naive repeating-source model behind the live bar.

The premise under test: records from the same source location are one
repeating shape, and the store already groups them that way. No `rich` here —
the model is display-independent, so it runs on a bare install.
"""

from __future__ import annotations

import pytest

from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    MAX_BARS_ENV_VAR,
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
