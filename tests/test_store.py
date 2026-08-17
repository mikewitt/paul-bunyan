"""Most tests here are parametrized across every installed RecordStore
backend via the `store` fixture. Not all of them: the schema-parity checks,
the on-disk-file guards (an older schema, a rejected file, reopening a fresh
one) and the `recent()` default-limit constant are SQLite-specific — they
construct a `SQLiteRecordStore` directly, because they are about that
backend's file format and `PRAGMA table_info`, not about the `RecordStore`
interface a second backend would also implement."""

from __future__ import annotations

import gc
import sqlite3
import time

import pytest

from lumberjack.schema import SourceKey
from lumberjack.store import (
    _COLUMNS,
    DEFAULT_RECENT_LIMIT,
    SQLiteRecordStore,
    WorkerKey,
)


def test_append_and_recent(store, make_row):
    store.append([make_row(message="one"), make_row(message="two")])
    rows = store.recent()
    assert [r.message for r in rows] == ["one", "two"]
    assert all(hasattr(r, "id") for r in rows)


def test_recent_respects_n(store, make_row):
    store.append([make_row(message=str(i)) for i in range(5)])
    rows = store.recent(n=2)
    assert [r.message for r in rows] == ["3", "4"]


def test_recent_respects_since(store, make_row):
    now = time.time()
    store.append(
        [
            make_row(created=now - 100, message="old"),
            make_row(created=now, message="new"),
        ]
    )
    rows = store.recent(since=now - 10)
    assert [r.message for r in rows] == ["new"]


def test_count_by_template(store, make_row):
    """Grouped by template id, unwindowed and windowed: the unwindowed count
    covers everything ever written, the windowed one only what's recent
    enough to matter for a live bar."""
    now = time.time()
    store.append(
        [
            make_row(created=now - 3600, template_id=1),
            make_row(created=now, template_id=1),
            make_row(created=now, template_id=2),
        ]
    )
    assert store.count_by_template() == {1: 2, 2: 1}
    assert store.count_by_template(window_seconds=60) == {1: 1, 2: 1}


def test_count_by_source(store, make_row):
    """Grouped by source location, unwindowed and windowed, mirroring
    `test_count_by_template` above."""
    now = time.time()
    store.append(
        [
            make_row(created=now - 3600, pathname="a.py", lineno=1, func_name="f"),
            make_row(created=now, pathname="a.py", lineno=1, func_name="f"),
            make_row(created=now, pathname="b.py", lineno=2, func_name="g"),
        ]
    )
    assert store.count_by_source() == {
        SourceKey("a.py", 1, "f"): 2,
        SourceKey("b.py", 2, "g"): 1,
    }
    assert store.count_by_source(window_seconds=60) == {
        SourceKey("a.py", 1, "f"): 1,
        SourceKey("b.py", 2, "g"): 1,
    }


# --- incremental counting --------------------------------------------------


def test_count_by_source_since_returns_only_newer_rows(store, make_row):
    store.append([make_row(pathname="a.py", lineno=1, func_name="f")])
    first = store.count_by_source_since(0)
    assert first.counts == {SourceKey("a.py", 1, "f"): 1}

    store.append([make_row(pathname="a.py", lineno=1, func_name="f") for _ in range(3)])
    second = store.count_by_source_since(first.last_id)
    assert second.counts == {SourceKey("a.py", 1, "f"): 3}
    assert second.last_id > first.last_id


@pytest.mark.parametrize(
    "seed_row, method_name, result_attr",
    [
        pytest.param(
            lambda make_row, make_task_row: make_row(),
            "count_by_source_since",
            "counts",
            id="count_by_source_since",
        ),
        pytest.param(
            lambda make_row, make_task_row: make_task_row(1, "start"),
            "task_events_since",
            "events",
            id="task_events_since",
        ),
    ],
)
def test_the_watermark_holds_when_nothing_arrived(
    store, make_row, make_task_row, seed_row, method_name, result_attr
):
    store.append([seed_row(make_row, make_task_row)])
    method = getattr(store, method_name)
    first = method(0)
    again = method(first.last_id)
    assert not getattr(again, result_attr)
    assert again.last_id == first.last_id, "an empty delta must not rewind"


@pytest.mark.parametrize(
    "seed_row, method_name, result_attr, extra_rows",
    [
        pytest.param(
            lambda make_row, make_task_row: make_task_row(1, "start"),
            "task_events_since",
            "events",
            lambda make_row, make_task_row: [
                make_row(message="plain") for _ in range(5)
            ],
            id="task watermark advances past ordinary rows",
        ),
        pytest.param(
            lambda make_row, make_task_row: make_row(),
            "count_by_source_since",
            "counts",
            lambda make_row, make_task_row: [
                make_task_row(1, "update") for _ in range(3)
            ],
            id="source watermark advances past task rows",
        ),
    ],
)
def test_the_watermark_advances_past_rows_the_method_does_not_count(
    store, make_row, make_task_row, seed_row, method_name, result_attr, extra_rows
):
    """The watermark spans every row in range, not just the ones the method
    itself counts — otherwise the rows it skips would be rescanned on every
    later poll, and the range would only grow."""
    store.append([seed_row(make_row, make_task_row)])
    method = getattr(store, method_name)
    first = method(0)
    store.append(extra_rows(make_row, make_task_row))
    second = method(first.last_id)
    assert not getattr(second, result_attr), "expected nothing new"
    assert second.last_id > first.last_id, "the watermark stalled"


def test_evict_before(store, make_row):
    now = time.time()
    store.append(
        [
            make_row(created=now - 100, message="old"),
            make_row(created=now, message="new"),
        ]
    )
    evicted = store.evict(before=now - 10)
    assert evicted == 1
    assert [r.message for r in store.recent()] == ["new"]


@pytest.mark.parametrize(
    "total_rows, keep_last, expected_evicted, survivors",
    [
        pytest.param(5, 2, 3, ["3", "4"], id="keeps exactly the newest N"),
        pytest.param(3, 0, 3, [], id="zero clears the store"),
        pytest.param(2, 100, 0, ["0", "1"], id="beyond the row count deletes nothing"),
    ],
)
def test_evict_keep_last(
    store, make_row, total_rows, keep_last, expected_evicted, survivors
):
    """`keep_last` is a floor, not a target: asking for more than exists
    deletes nothing — the cutoff subquery returns NULL there, and `id < NULL`
    matches no row — and asking for zero must clear the store rather than
    being mistaken for "no limit"."""
    store.append([make_row(message=str(i)) for i in range(total_rows)])
    assert store.evict(keep_last=keep_last) == expected_evicted
    assert [r.message for r in store.recent()] == survivors


def test_evict_requires_exactly_one_arg(store):
    with pytest.raises(ValueError):
        store.evict()
    with pytest.raises(ValueError):
        store.evict(before=1.0, keep_last=1)


def test_templates_returns_distinct_non_null(store, make_row):
    store.append(
        [make_row(template_id=1), make_row(template_id=1), make_row(template_id=None)]
    )
    assert sorted(store.templates()) == [1]


def test_append_empty_is_noop(store):
    store.append([])
    assert store.recent() == []


def test_a_store_file_from_an_older_schema_fails_loudly(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` leaves an old table alone, so every INSERT
    would raise — and both the pump and teardown swallow exceptions by design,
    so the run would record nothing and say nothing. Pre-1.0 lets us break such
    a file; it does not let us break it in silence.

    The fixture is the real pre-rename table, not a toy: it keeps every column
    the indexes touch, so `_SCHEMA` itself runs clean and only the writes would
    have failed. A table missing `created` would trip `CREATE INDEX` instead
    and prove nothing about the case that actually goes quiet.
    """
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    added_since = {
        "asyncio_task_name",
        "asyncio_task_id",
        "task_label",
        "task_event",
        "progress_current",
        "progress_total",
    }
    old_columns = [c for c in _COLUMNS if c not in added_since] + ["task_name"]
    conn.execute(
        "CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(f"{c} TEXT" for c in old_columns)
        + ")"
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="different lumberjack schema"):
        SQLiteRecordStore(path)


def test_a_fresh_store_file_opens_and_reopens(tmp_path, make_row):
    """The guard must not fire on a file lumberjack itself just wrote."""
    path = str(tmp_path / "fresh.db")
    first = SQLiteRecordStore(path)
    first.append([make_row(message="written")])
    first.close()

    second = SQLiteRecordStore(path)
    try:
        assert [r.message for r in second.recent()] == ["written"]
    finally:
        second.close()


def test_a_rejected_store_file_does_not_leak_its_connection(tmp_path, recwarn):
    """Raising from `__init__` must still close the connection it opened.
    From 3.13 an unclosed one emits a ResourceWarning when collected, and
    `filterwarnings = ["error"]` turns that into a failure in whichever
    unrelated test happens to trigger the collection."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, msg TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError):
        SQLiteRecordStore(path)
    gc.collect()
    assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]


# --- the tracking API's own rows -------------------------------------------


def test_task_events_since_returns_only_task_rows(store, make_row, make_task_row):
    store.append([make_row(message="ordinary"), make_task_row(1, "start")])
    delta = store.task_events_since(0)
    assert [e.event for e in delta.events] == ["start"]
    assert delta.events[0].task_id == 1


def test_task_events_since_returns_them_oldest_first(store, make_task_row):
    store.append(
        [
            make_task_row(1, "start"),
            make_task_row(1, "update", progress_current=5),
            make_task_row(1, "end", progress_current=9),
        ]
    )
    assert [e.event for e in store.task_events_since(0).events] == [
        "start",
        "update",
        "end",
    ]


def test_task_events_since_carries_every_column_a_bar_needs(store, make_task_row):
    store.append(
        [
            make_task_row(
                7,
                "update",
                label="reindex",
                parent_task_id=3,
                progress_current=40,
                progress_total=100,
            )
        ]
    )
    (event,) = store.task_events_since(0).events
    assert (event.task_id, event.parent_task_id) == (7, 3)
    assert (event.label, event.event) == ("reindex", "update")
    assert (event.current, event.total) == (40, 100)


def test_the_source_delta_ignores_task_rows(store, make_row, make_task_row):
    """The tracking API knows its own exact numbers and gets its own bars.
    Counting its rows here too would draw a source-location bar beside every
    named one — visible on the first run of examples/tracking.py."""
    store.append(
        [
            make_row(pathname="a.py", lineno=1, func_name="f"),
            make_task_row(1, "start", pathname="a.py", lineno=1, func_name="f"),
        ]
    )
    assert store.count_by_source_since(0).counts == {SourceKey("a.py", 1, "f"): 1}


def test_the_source_delta_reports_which_workers_ran_each_line(store, make_row):
    """Structural analysis needs this to tell one loop nested inside another
    from two unrelated loops on two threads."""
    store.append(
        [
            make_row(lineno=1, thread=7),
            make_row(lineno=1, thread=9),
            make_row(lineno=1, thread=7),
            make_row(lineno=2, thread=7),
        ]
    )
    workers = store.count_by_source_since(0).workers
    assert {w.thread for w in workers[SourceKey("/tmp/foo.py", 1, "bar")]} == {7, 9}
    assert {w.thread for w in workers[SourceKey("/tmp/foo.py", 2, "bar")]} == {7}


def test_a_worker_is_process_thread_and_asyncio_task(store, make_row):
    """Thread ids repeat across processes, and one event loop runs many tasks
    on one thread, so no single column identifies a worker."""
    store.append(
        [
            make_row(process=1, thread=1, asyncio_task_id=None),
            make_row(process=2, thread=1, asyncio_task_id=None),
            make_row(process=1, thread=1, asyncio_task_id=5),
        ]
    )
    workers = store.count_by_source_since(0).workers[
        SourceKey("/tmp/foo.py", 10, "bar")
    ]
    assert workers == frozenset(
        {WorkerKey(1, 1, None), WorkerKey(2, 1, None), WorkerKey(1, 1, 5)}
    )


def test_the_source_delta_still_folds_per_worker_rows_back_together(store, make_row):
    """Grouping by worker splits each source into several rows; the counts and
    the span a caller reads must still describe the source as a whole."""
    store.append(
        [
            make_row(thread=1, created=100.0),
            make_row(thread=2, created=101.0),
            make_row(thread=1, created=102.0),
        ]
    )
    delta = store.count_by_source_since(0)
    key = SourceKey("/tmp/foo.py", 10, "bar")
    assert delta.counts[key] == 3
    assert (delta.first_at[key], delta.last_at[key]) == (100.0, 102.0)


# --- recent() is bounded by default (#7) ------------------------------------


def test_the_default_limit_is_a_documented_number():
    """Pinned as a literal on purpose. Every other test here compares against
    the constant, so they all move together if it changes — but the value is
    quoted in the README and in `recent()`'s docstring, which makes it a
    contract rather than a tuning knob. Changing it should mean changing
    those too, and this is what makes that happen."""
    assert DEFAULT_RECENT_LIMIT == 1000


def test_the_default_keeps_the_newest_records(store, make_row):
    """Bounded from the newest end, oldest-first within that — a tail, not a
    head. Returning the *first* 1000 of a long run would be worse than
    useless.

    `recent()` is the documented way to query captured records, and the
    unbounded form builds one dataclass per row — multi-second and half a
    million objects at the retention target, from a call that looks free —
    so the default bound is asserted here too."""
    store.append([make_row(message=str(i)) for i in range(DEFAULT_RECENT_LIMIT + 3)])
    rows = store.recent()
    assert len(rows) == DEFAULT_RECENT_LIMIT
    assert rows[-1].message == str(DEFAULT_RECENT_LIMIT + 2)
    assert rows[0].message == "3"


def test_recent_none_still_means_everything(store, make_row):
    """The escape hatch stays, it just has to be asked for."""
    store.append([make_row() for _ in range(DEFAULT_RECENT_LIMIT + 25)])
    assert len(store.recent(n=None)) == DEFAULT_RECENT_LIMIT + 25


def test_the_default_does_not_override_an_explicit_since(store, make_row):
    """`n` and `since` compose as "the last n of those at or after since",
    and the default `n` must not change what `since` alone would select."""
    now = time.time()
    store.append(
        [make_row(created=now - 100, message="old")]
        + [make_row(created=now, message=str(i)) for i in range(5)]
    )
    assert [r.message for r in store.recent(since=now - 10)] == list("01234")
