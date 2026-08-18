from __future__ import annotations

import dataclasses
import logging
import sys
from collections.abc import Callable

import pytest

from lumberjack.schema import EXTRA_KEY, LogRecordRow, SourceKey, StoredRecord
from lumberjack.store import _COLUMNS, SQLiteRecordStore


def test_from_log_record_basic_fields(make_log_record):
    record = make_log_record()
    row = LogRecordRow.from_log_record(record)
    assert row.logger_name == "test.logger"
    assert row.level_name == "INFO"
    assert row.level_no == logging.INFO
    assert row.message == "hello world"
    assert row.lineno == 42


def test_from_log_record_reserved_fields_default_to_none(make_log_record):
    record = make_log_record()
    row = LogRecordRow.from_log_record(record)
    assert row.asyncio_task_name is None
    assert row.asyncio_task_id is None
    assert row.task_id is None
    assert row.parent_task_id is None
    assert row.template_id is None


# --- the `extra=` seam the tracking API writes through ----------------------


@pytest.fixture
def record_carrying(
    make_log_record: Callable[..., logging.LogRecord],
) -> Callable[[object], logging.LogRecord]:
    """A record with `payload` under the extra key.

    `setattr` after construction is precisely what `logging` does with
    `extra=` — `Logger.makeRecord` copies the mapping onto `record.__dict__`.
    """

    def _carrying(payload: object) -> logging.LogRecord:
        record = make_log_record()
        setattr(record, EXTRA_KEY, payload)
        return record

    return _carrying


def test_a_task_event_populates_the_progress_columns(make_task_event, record_carrying):
    row = LogRecordRow.from_log_record(record_carrying(make_task_event()))
    assert (row.task_label, row.task_event) == ("reindex", "update")
    assert (row.task_id, row.parent_task_id) == (7, 3)
    assert (row.progress_current, row.progress_total) == (40, 100)


def test_a_task_event_without_progress_leaves_those_columns_null(
    make_task_event, record_carrying
):
    """An indeterminate task is the common case: `task()` with no total."""
    event = make_task_event(
        label="migrate",
        kind="start",
        task_id=1,
        parent_task_id=None,
        current=None,
        total=None,
    )
    row = LogRecordRow.from_log_record(record_carrying(event))
    assert (row.task_label, row.task_event) == ("migrate", "start")
    assert (row.progress_current, row.progress_total) == (None, None)
    assert row.parent_task_id is None


def test_a_foreign_attribute_under_our_key_is_ignored(record_carrying):
    """Somebody else's `extra={"lumberjack": ...}` must not crash `emit()` for
    every record in the process — it degrades to a record with no task data."""
    row = LogRecordRow.from_log_record(record_carrying({"label": "not ours"}))
    assert row.task_label is None
    assert row.task_event is None
    assert row.task_id is None


def test_from_log_record_captures_exception_text(make_log_record):
    try:
        raise ValueError("bad")
    except ValueError:
        record = make_log_record(msg="failed", args=(), exc_info=sys.exc_info())
    row = LogRecordRow.from_log_record(record)
    assert row.exc_text is not None
    assert "ValueError: bad" in row.exc_text


def test_stored_record_adds_id(make_log_record):
    record = make_log_record()
    row = LogRecordRow.from_log_record(record)
    field_values = {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}
    stored = StoredRecord(id=1, **field_values)
    assert stored.id == 1
    assert stored.message == row.message


# --- schema/column parity ---------------------------------------------------
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


# A record's `pathname` is whatever the process that emitted it wrote down,
# so a store read back on another platform holds paths of the other flavour.
# The host's `os.path` splits on the host's separator only, which is why the
# label is built with `ntpath` on every platform.


@pytest.mark.parametrize(
    ("pathname", "expected"),
    [
        ("/home/me/proj/worker.py", "worker.py:42 process()"),
        ("C:\\Users\\me\\proj\\worker.py", "worker.py:42 process()"),
        ("worker.py", "worker.py:42 process()"),
    ],
    ids=["posix", "windows", "bare"],
)
def test_source_key_format_truncates_either_flavour_of_path(pathname, expected):
    assert SourceKey(pathname, 42, "process").format() == expected
