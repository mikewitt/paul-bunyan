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

from lumberjack.schema import LogRecordRow, SourceKey, StoredRecord

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
    "task_name",
    "task_id",
    "parent_task_id",
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
    task_name       TEXT,
    task_id         INTEGER,
    parent_task_id  INTEGER,
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
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

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
        # Full GROUP BY, and the renderer calls this every 200ms; needs to
        # become incremental before Phase 4. lumberjack: see issue #5
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

    def evict(
        self, *, before: float | None = None, keep_last: int | None = None
    ) -> int:
        if (before is None) == (keep_last is None):
            raise ValueError("evict() requires exactly one of before, keep_last")
        with self._lock:
            if before is not None:
                cur = self._conn.execute(
                    "DELETE FROM records WHERE created < ?", (before,)
                )
            else:
                # NOT IN against a large subquery; ~2.2s at 1M rows, holding
                # the lock against every reader. lumberjack: see issue #6
                cur = self._conn.execute(
                    "DELETE FROM records WHERE id NOT IN "
                    "(SELECT id FROM records ORDER BY id DESC LIMIT ?)",
                    (keep_last,),
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
