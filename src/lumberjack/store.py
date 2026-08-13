"""Pluggable record storage.

`RecordStore` is the interface analysis and rendering talk to; they never
touch a backend's native representation directly. `SQLiteRecordStore` is the
zero-dependency default. Backends share one parametrized test suite
(tests/conftest.py `store` fixture) so a future DuckDB backend slots in
without changing test code.
"""

from __future__ import annotations

import abc
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from operator import attrgetter
from typing import NamedTuple

from lumberjack.schema import LogRecordRow, SourceKey, StoredRecord


class SourceDelta(NamedTuple):
    """Records appended since a watermark, grouped by source location.

    `last_id` is where the caller should resume from. It comes back in the
    same query as the counts rather than from a second `MAX(id)` call, so a
    row appended between the two can't be counted twice or skipped.
    """

    counts: Mapping[SourceKey, int]
    last_id: int


_COLUMNS = (
    "logger_name",
    "level_name",
    "level_no",
    "msg",
    "message",
    "pathname",
    "filename",
    "module",
    "func_name",
    "lineno",
    "created",
    "thread",
    "thread_name",
    "process",
    "process_name",
    "exc_text",
    "stack_text",
    "asyncio_task_name",
    "asyncio_task_id",
    "task_id",
    "parent_task_id",
    "task_label",
    "task_event",
    "progress_current",
    "progress_total",
    "template_id",
)
_GET_COLUMNS = attrgetter(*_COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
_INSERT_SQL = f"INSERT INTO records ({', '.join(_COLUMNS)}) VALUES ({_PLACEHOLDERS})"


class RecordStore(abc.ABC):
    # These methods carry the contract every backend implements against, and
    # none of it is written down yet. lumberjack: see issue #15

    @abc.abstractmethod
    def append(self, rows: Sequence[LogRecordRow]) -> None: ...

    @abc.abstractmethod
    def recent(
        self, n: int | None = None, since: float | None = None
    ) -> Sequence[StoredRecord]: ...

    @abc.abstractmethod
    def count_by_template(
        self, window_seconds: float | None = None
    ) -> Mapping[int | None, int]: ...

    @abc.abstractmethod
    def count_by_source(
        self, window_seconds: float | None = None
    ) -> Mapping[SourceKey, int]: ...

    @abc.abstractmethod
    def count_by_source_since(self, after_id: int) -> SourceDelta: ...

    @abc.abstractmethod
    def evict(
        self, *, before: float | None = None, keep_last: int | None = None
    ) -> int: ...

    @abc.abstractmethod
    def templates(self) -> Sequence[int]: ...

    @abc.abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    logger_name     TEXT NOT NULL,
    level_name      TEXT NOT NULL,
    level_no        INTEGER NOT NULL,
    msg             TEXT NOT NULL,
    message         TEXT NOT NULL,
    pathname        TEXT NOT NULL,
    filename        TEXT NOT NULL,
    module          TEXT NOT NULL,
    func_name       TEXT NOT NULL,
    lineno          INTEGER NOT NULL,
    created         REAL NOT NULL,
    thread          INTEGER NOT NULL,
    thread_name     TEXT NOT NULL,
    process         INTEGER NOT NULL,
    process_name    TEXT NOT NULL,
    exc_text        TEXT,
    stack_text      TEXT,
    asyncio_task_name TEXT,
    asyncio_task_id INTEGER,
    task_id         INTEGER,
    parent_task_id  INTEGER,
    task_label      TEXT,
    task_event      TEXT,
    progress_current INTEGER,
    progress_total  INTEGER,
    template_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_records_created     ON records(created);
CREATE INDEX IF NOT EXISTS idx_records_template_id ON records(template_id);
CREATE INDEX IF NOT EXISTS idx_records_source
    ON records(pathname, lineno, func_name);
"""


class SQLiteRecordStore(RecordStore):
    """Stdlib `sqlite3`-backed store. Raw SQL, no ORM."""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._reject_a_foreign_schema()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _reject_a_foreign_schema(self) -> None:
        """Fail loudly on a store file an older lumberjack wrote.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a file
        predating a schema change keeps its old columns and every `INSERT`
        raises. Nothing would surface that: the flush pump swallows exceptions
        per tick by design, and so does teardown's final flush — the run would
        record nothing and say nothing. Pre-1.0 means we may break such a file;
        it does not mean breaking it in silence.

        Checked before `_SCHEMA` runs, so the message names the mismatch rather
        than whichever `CREATE INDEX` happens to trip over a missing column.
        """
        found = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(records)").fetchall()
        }
        if not found:
            return  # no table yet, which is the ordinary case
        missing = sorted(set(_COLUMNS) - found)
        if missing:
            raise RuntimeError(
                f"{self._path!r} holds a `records` table from a different "
                f"lumberjack schema, missing {missing}. lumberjack is pre-1.0 "
                "with no migration path yet: delete the file or point at a new "
                "one."
            )

    def append(self, rows: Sequence[LogRecordRow]) -> None:
        if not rows:
            return
        values = list(map(_GET_COLUMNS, rows))
        with self._lock:
            self._conn.executemany(_INSERT_SQL, values)
            self._conn.commit()

    def _row_to_stored(self, row: sqlite3.Row) -> StoredRecord:
        kwargs = {col: row[col] for col in _COLUMNS}
        return StoredRecord(id=row["id"], **kwargs)

    def recent(
        self, n: int | None = None, since: float | None = None
    ) -> Sequence[StoredRecord]:
        # n=None materializes the whole store. lumberjack: see issue #7
        sql = "SELECT * FROM records"
        params: list[object] = []
        if since is not None:
            sql += " WHERE created >= ?"
            params.append(since)
        sql += " ORDER BY id DESC"
        if n is not None:
            sql += " LIMIT ?"
            params.append(n)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_stored(r) for r in reversed(rows)]

    def count_by_template(
        self, window_seconds: float | None = None
    ) -> Mapping[int | None, int]:
        sql = "SELECT template_id, COUNT(*) AS cnt FROM records"
        params: list[object] = []
        if window_seconds is not None:
            sql += " WHERE created >= ?"
            params.append(time.time() - window_seconds)
        sql += " GROUP BY template_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {r["template_id"]: r["cnt"] for r in rows}

    def count_by_source(
        self, window_seconds: float | None = None
    ) -> Mapping[SourceKey, int]:
        """Whole-store tally. O(rows) — for one-off queries, not a redraw loop.

        Anything polling on a timer wants `count_by_source_since()` instead.
        """
        sql = "SELECT pathname, lineno, func_name, COUNT(*) AS cnt FROM records"
        params: list[object] = []
        if window_seconds is not None:
            sql += " WHERE created >= ?"
            params.append(time.time() - window_seconds)
        sql += " GROUP BY pathname, lineno, func_name"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {
            SourceKey(r["pathname"], r["lineno"], r["func_name"]): r["cnt"]
            for r in rows
        }

    def count_by_source_since(self, after_id: int) -> SourceDelta:
        """Group only the rows appended after `after_id`.

        Costs what arrived since the last call rather than what the store
        holds, which is what lets a redraw run five times a second against a
        million rows.

        `NOT INDEXED` is load-bearing, not leftover debugging. Left to itself
        SQLite serves the GROUP BY from `idx_records_source` as a covering
        index — no sort, but a scan of every row in the store, which is the
        cost this method exists to avoid. `NOT INDEXED` rules that out while
        still allowing the INTEGER PRIMARY KEY, so the plan becomes a rowid
        range seek plus a sort of the delta. Measured over 1M rows: 32ms
        against 0.5ms.
        """
        sql = (
            "SELECT pathname, lineno, func_name, COUNT(*) AS cnt, MAX(id) AS max_id "
            "FROM records NOT INDEXED WHERE id > ? "
            "GROUP BY pathname, lineno, func_name"
        )
        with self._lock:
            rows = self._conn.execute(sql, (after_id,)).fetchall()
        counts = {
            SourceKey(r["pathname"], r["lineno"], r["func_name"]): r["cnt"]
            for r in rows
        }
        # No new rows leaves the watermark where it was; never move it back.
        return SourceDelta(counts, max((r["max_id"] for r in rows), default=after_id))

    def evict(
        self, *, before: float | None = None, keep_last: int | None = None
    ) -> int:
        if (before is None) == (keep_last is None):
            raise ValueError("evict() requires exactly one of before, keep_last")
        # keep_last first: the guard above already established exactly one is
        # set, but testing it explicitly is what narrows it to an int for the
        # arithmetic below.
        with self._lock:
            if keep_last is None:
                cur = self._conn.execute(
                    "DELETE FROM records WHERE created < ?", (before,)
                )
            elif keep_last <= 0:
                cur = self._conn.execute("DELETE FROM records")
            else:
                # Resolve the cutoff id first, then delete by range. The
                # obvious `id NOT IN (SELECT ... LIMIT ?)` builds and probes a
                # set of every surviving id; this walks the primary key once.
                # A store holding fewer than keep_last rows makes the subquery
                # NULL, so nothing matches and nothing is deleted — correct.
                cur = self._conn.execute(
                    "DELETE FROM records WHERE id < "
                    "(SELECT id FROM records ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (keep_last - 1,),
                )
            self._conn.commit()
            return cur.rowcount

    def templates(self) -> Sequence[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT template_id FROM records WHERE template_id IS NOT NULL"
            ).fetchall()
        return [r["template_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
