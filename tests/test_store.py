"""Parametrized across every installed RecordStore backend via the `store` fixture."""

from __future__ import annotations

import time

import pytest

from lumberjack.schema import LogRecordRow, SourceKey


def _row(**overrides: object) -> LogRecordRow:
    fields: dict[str, object] = dict(
        logger_name="test",
        level_name="INFO",
        level_no=20,
        msg="msg %s",
        message="msg hello",
        pathname="/tmp/foo.py",
        filename="foo.py",
        module="foo",
        func_name="bar",
        lineno=10,
        created=time.time(),
        thread=1,
        thread_name="MainThread",
        process=100,
        process_name="MainProcess",
        exc_text=None,
        stack_text=None,
        task_name=None,
        task_id=None,
        parent_task_id=None,
        template_id=None,
    )
    fields.update(overrides)
    return LogRecordRow(**fields)  # type: ignore[arg-type]


def test_append_and_recent(store):
    store.append([_row(message="one"), _row(message="two")])
    rows = store.recent()
    assert [r.message for r in rows] == ["one", "two"]
    assert all(hasattr(r, "id") for r in rows)


def test_recent_respects_n(store):
    store.append([_row(message=str(i)) for i in range(5)])
    rows = store.recent(n=2)
    assert [r.message for r in rows] == ["3", "4"]


def test_recent_respects_since(store):
    now = time.time()
    store.append(
        [_row(created=now - 100, message="old"), _row(created=now, message="new")]
    )
    rows = store.recent(since=now - 10)
    assert [r.message for r in rows] == ["new"]


def test_tail_returns_oldest_to_newest(store):
    store.append([_row(message=str(i)) for i in range(3)])
    rows = store.tail(2)
    assert [r.message for r in rows] == ["1", "2"]


def test_count_by_template(store):
    store.append([_row(template_id=1), _row(template_id=1), _row(template_id=2)])
    counts = store.count_by_template()
    assert counts[1] == 2
    assert counts[2] == 1


def test_count_by_source(store):
    store.append(
        [
            _row(pathname="a.py", lineno=1, func_name="f"),
            _row(pathname="a.py", lineno=1, func_name="f"),
            _row(pathname="b.py", lineno=2, func_name="g"),
        ]
    )
    counts = store.count_by_source()
    assert counts[SourceKey("a.py", 1, "f")] == 2
    assert counts[SourceKey("b.py", 2, "g")] == 1


def test_evict_before(store):
    now = time.time()
    store.append(
        [_row(created=now - 100, message="old"), _row(created=now, message="new")]
    )
    evicted = store.evict(before=now - 10)
    assert evicted == 1
    assert [r.message for r in store.recent()] == ["new"]


def test_evict_keep_last(store):
    store.append([_row(message=str(i)) for i in range(5)])
    evicted = store.evict(keep_last=2)
    assert evicted == 3
    assert [r.message for r in store.recent()] == ["3", "4"]


def test_evict_requires_exactly_one_arg(store):
    with pytest.raises(ValueError):
        store.evict()
    with pytest.raises(ValueError):
        store.evict(before=1.0, keep_last=1)


def test_templates_returns_distinct_non_null(store):
    store.append([_row(template_id=1), _row(template_id=1), _row(template_id=None)])
    assert sorted(store.templates()) == [1]


def test_append_empty_is_noop(store):
    store.append([])
    assert store.recent() == []
