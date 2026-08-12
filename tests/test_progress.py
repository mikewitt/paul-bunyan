"""Tests for the naive repeating-source model behind the live bar.

The premise under test: records from the same source location are one
repeating shape, and the store already groups them that way. No `rich` here —
the model is display-independent, so it runs on a bare install.
"""

from __future__ import annotations

from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    BarState,
    RepeatingSourceModel,
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
