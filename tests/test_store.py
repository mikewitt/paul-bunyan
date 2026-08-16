"""Parametrized across every installed RecordStore backend via the `store` fixture."""

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
    store.append(
        [make_row(template_id=1), make_row(template_id=1), make_row(template_id=2)]
    )
    counts = store.count_by_template()
    assert counts[1] == 2
    assert counts[2] == 1


def test_count_by_source(store, make_row):
    store.append(
        [
            make_row(pathname="a.py", lineno=1, func_name="f"),
            make_row(pathname="a.py", lineno=1, func_name="f"),
            make_row(pathname="b.py", lineno=2, func_name="g"),
        ]
    )
    counts = store.count_by_source()
    assert counts[SourceKey("a.py", 1, "f")] == 2
    assert counts[SourceKey("b.py", 2, "g")] == 1


def test_count_by_template_within_a_window(store, make_row):
    now = time.time()
    store.append(
        [
            make_row(created=now - 3600, template_id=1),
            make_row(created=now, template_id=1),
        ]
    )
    assert store.count_by_template(window_seconds=60) == {1: 1}


def test_count_by_source_within_a_window(store, make_row):
    now = time.time()
    store.append(
        [
            make_row(created=now - 3600, pathname="a.py", lineno=1, func_name="f"),
            make_row(created=now, pathname="a.py", lineno=1, func_name="f"),
            make_row(created=now, pathname="a.py", lineno=1, func_name="f"),
        ]
    )
    assert store.count_by_source(window_seconds=60) == {SourceKey("a.py", 1, "f"): 2}


# --- incremental counting --------------------------------------------------


def test_count_by_source_since_returns_only_newer_rows(store, make_row):
    store.append([make_row(pathname="a.py", lineno=1, func_name="f")])
    first = store.count_by_source_since(0)
    assert first.counts == {SourceKey("a.py", 1, "f"): 1}

    store.append([make_row(pathname="a.py", lineno=1, func_name="f") for _ in range(3)])
    second = store.count_by_source_since(first.last_id)
    assert second.counts == {SourceKey("a.py", 1, "f"): 3}
    assert second.last_id > first.last_id


def test_count_by_source_since_holds_the_watermark_when_nothing_arrived(
    store, make_row
):
    store.append([make_row()])
    first = store.count_by_source_since(0)
    again = store.count_by_source_since(first.last_id)
    assert again.counts == {}
    assert again.last_id == first.last_id, "an empty delta must not rewind"


def test_count_by_source_since_from_zero_sees_everything(store, make_row):
    store.append([make_row() for _ in range(4)])
    assert sum(store.count_by_source_since(0).counts.values()) == 4


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


def test_evict_keep_last(store, make_row):
    store.append([make_row(message=str(i)) for i in range(5)])
    evicted = store.evict(keep_last=2)
    assert evicted == 3
    assert [r.message for r in store.recent()] == ["3", "4"]


def test_evict_keep_last_zero_clears_the_store(store, make_row):
    store.append([make_row() for _ in range(3)])
    assert store.evict(keep_last=0) == 3
    assert store.recent() == []


def test_evict_keep_last_beyond_the_row_count_deletes_nothing(store, make_row):
    # The cutoff subquery returns NULL here, and `id < NULL` matches no row.
    store.append([make_row(message=str(i)) for i in range(2)])
    assert store.evict(keep_last=100) == 0
    assert [r.message for r in store.recent()] == ["0", "1"]


def test_evict_keep_last_one_keeps_the_newest(store, make_row):
    store.append([make_row(message=str(i)) for i in range(4)])
    assert store.evict(keep_last=1) == 3
    assert [r.message for r in store.recent()] == ["3"]


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


# --- the created table matches the INSERT, SQLite-specific ------------


def test_insert_columns_and_created_table_agree():
    """Catches `_SCHEMA` drift, which the two checks above cannot see."""
    sqlite_store = SQLiteRecordStore(":memory:")
    try:
        rows = sqlite_store._conn.execute("PRAGMA table_info(records)").fetchall()
    finally:
        sqlite_store.close()
    assert {r["name"] for r in rows} == set(_COLUMNS) | {"id"}


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


def _task_row(make_row, task_id, event, **kw):
    return make_row(
        task_id=task_id,
        task_event=event,
        task_label=kw.pop("label", "job"),
        **kw,
    )


def test_task_events_since_returns_only_task_rows(store, make_row):
    store.append([make_row(message="ordinary"), _task_row(make_row, 1, "start")])
    delta = store.task_events_since(0)
    assert [e.event for e in delta.events] == ["start"]
    assert delta.events[0].task_id == 1


def test_task_events_since_returns_them_oldest_first(store, make_row):
    store.append(
        [
            _task_row(make_row, 1, "start"),
            _task_row(make_row, 1, "update", progress_current=5),
            _task_row(make_row, 1, "end", progress_current=9),
        ]
    )
    assert [e.event for e in store.task_events_since(0).events] == [
        "start",
        "update",
        "end",
    ]


def test_task_events_since_advances_past_ordinary_records(store, make_row):
    """The watermark spans every row in range, not just the task ones —
    otherwise ordinary records after the last task event would be rescanned
    on every poll, and the range would only grow."""
    store.append([_task_row(make_row, 1, "start")])
    first = store.task_events_since(0)
    store.append([make_row(message="plain") for _ in range(5)])
    second = store.task_events_since(first.last_id)
    assert second.events == ()
    assert second.last_id > first.last_id, "the watermark stalled behind plain rows"


def test_task_events_since_holds_the_watermark_when_nothing_arrived(store, make_row):
    store.append([_task_row(make_row, 1, "start")])
    first = store.task_events_since(0)
    again = store.task_events_since(first.last_id)
    assert again.events == ()
    assert again.last_id == first.last_id, "an empty delta must not rewind"


def test_task_events_since_carries_every_column_a_bar_needs(store, make_row):
    store.append(
        [
            _task_row(
                make_row,
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


def test_the_source_delta_ignores_task_rows(store, make_row):
    """The tracking API knows its own exact numbers and gets its own bars.
    Counting its rows here too would draw a source-location bar beside every
    named one — visible on the first run of examples/tracking.py."""
    store.append(
        [
            make_row(pathname="a.py", lineno=1, func_name="f"),
            _task_row(make_row, 1, "start", pathname="a.py", lineno=1, func_name="f"),
        ]
    )
    assert store.count_by_source_since(0).counts == {SourceKey("a.py", 1, "f"): 1}


def test_the_source_delta_watermark_advances_past_task_rows(store, make_row):
    """Excluding them by grouping rather than by WHERE: filtering them out of
    the range would leave the watermark behind a tail of task rows, and every
    later poll would rescan a range that only grows."""
    store.append([make_row()])
    first = store.count_by_source_since(0)
    store.append([_task_row(make_row, 1, "update") for _ in range(3)])
    second = store.count_by_source_since(first.last_id)
    assert second.counts == {}
    assert second.last_id > first.last_id, "the watermark stalled behind task rows"


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


def test_recent_is_bounded_by_default(store, make_row):
    """`recent()` is the documented way to query captured records, and the
    unbounded form builds one dataclass per row — multi-second and half a
    million objects at the retention target, from a call that looks free."""
    store.append([make_row(message=str(i)) for i in range(DEFAULT_RECENT_LIMIT + 25)])
    assert len(store.recent()) == DEFAULT_RECENT_LIMIT


def test_the_default_keeps_the_newest_records(store, make_row):
    """Bounded from the newest end, oldest-first within that — a tail, not a
    head. Returning the *first* 1000 of a long run would be worse than
    useless."""
    store.append([make_row(message=str(i)) for i in range(DEFAULT_RECENT_LIMIT + 3)])
    rows = store.recent()
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
