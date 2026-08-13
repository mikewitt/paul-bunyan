"""Parametrized across every installed RecordStore backend via the `store` fixture."""

from __future__ import annotations

import dataclasses
import time

import pytest

from lumberjack.schema import LogRecordRow, SourceKey, StoredRecord
from lumberjack.store import _COLUMNS, SQLiteRecordStore


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


# --- schema/column parity, SQLite-specific -------------------------------
#
# Three lists have to agree: the dataclass fields, the INSERT column tuple,
# and the CREATE TABLE. Nothing links them, and `kw_only=True` means a field
# with a default now constructs fine while silently never reaching the store.


def test_row_fields_and_insert_columns_agree():
    assert {f.name for f in dataclasses.fields(LogRecordRow)} == set(_COLUMNS)


def test_stored_record_is_a_row_plus_id():
    assert {f.name for f in dataclasses.fields(StoredRecord)} == set(_COLUMNS) | {"id"}


def test_insert_columns_and_created_table_agree():
    """Catches `_SCHEMA` drift, which the two checks above cannot see."""
    sqlite_store = SQLiteRecordStore(":memory:")
    try:
        rows = sqlite_store._conn.execute("PRAGMA table_info(records)").fetchall()
    finally:
        sqlite_store.close()
    assert {r["name"] for r in rows} == set(_COLUMNS) | {"id"}
