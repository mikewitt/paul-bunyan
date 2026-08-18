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


class WorkerKey(NamedTuple):
    """Which concurrent worker a record came from.

    All three parts are needed and none is redundant: thread ids repeat
    across processes, and an asyncio event loop runs many tasks on one
    thread. `asyncio_task_id` is None outside a running loop, which is the
    ordinary case and is a perfectly good worker identity on its own.
    """

    process: int
    thread: int
    asyncio_task_id: int | None


class SourceDelta(NamedTuple):
    """Records appended since a watermark, grouped by source location.

    `last_id` is where the caller should resume from. It comes back in the
    same query as the counts rather than from a second `MAX(id)` call, so a
    row appended between the two can't be counted twice or skipped.

    `first_at` and `last_at` are the oldest and newest `created` in each
    group. They are what lets a reader work out how fast a source is
    repeating without a second query — a source's own recurrence interval is
    its loop's period.

    `workers` is which concurrent workers each source was seen on, which is
    what tells structural analysis that two sources *could* be one loop
    nested in another rather than two unrelated loops on two threads.

    All three carry the same keys as `counts`, always.
    """

    counts: Mapping[SourceKey, int]
    last_id: int
    first_at: Mapping[SourceKey, float]
    last_at: Mapping[SourceKey, float]
    workers: Mapping[SourceKey, frozenset[WorkerKey]]


class TaskEventRow(NamedTuple):
    """One row the tracking API wrote, as the display cares about it."""

    task_id: int
    parent_task_id: int | None
    label: str
    event: str
    current: int | None
    total: int | None


class TaskDelta(NamedTuple):
    """Task rows since a watermark, plus where to resume from.

    `last_id` spans *all* rows in the range, not just the task ones, so
    ordinary records between task events are never rescanned.
    """

    events: Sequence[TaskEventRow]
    last_id: int


#: How many records `recent()` returns when the caller does not say.
#:
#: Bounded by default because `recent()` is the documented way to query
#: captured records, and the unbounded form materializes one dataclass per
#: row: at the ~1M-record retention target that is roughly nine seconds and
#: half a million live objects, from a call that looks free. A cap is the
#: wrong answer for the rare caller who genuinely wants everything and the
#: right one for everybody else, so `n=None` still means "all of it" — it
#: just has to be asked for now.
DEFAULT_RECENT_LIMIT = 1000

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
_COLS = ", ".join(_COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
# Both halves come from `_COLUMNS`, the literal tuple above; every *value*
# goes through a `?`. The parity tests pin `_COLUMNS` against the real table.
_INSERT_SQL = f"INSERT INTO records ({_COLS}) VALUES ({_PLACEHOLDERS})"  # nosec B608


class RecordStore(abc.ABC):
    # These methods carry the contract every backend implements against, and
    # none of it is written down yet. lumberjack: see issue #15

    @abc.abstractmethod
    def append(self, rows: Sequence[LogRecordRow]) -> None: ...

    @abc.abstractmethod
    def recent(
        self,
        n: int | None = DEFAULT_RECENT_LIMIT,
        since: float | None = None,
    ) -> Sequence[StoredRecord]:
        """The most recent records, oldest first.

        `n` bounds how many are returned, counting back from the newest;
        `None` means every match, which at the retention target is a
        multi-second call and half a million objects. `since` filters to
        records at or after a `time.time()` timestamp. Given both, the two
        compose as "the last `n` of those at or after `since`".

        Oldest-first even though the newest are the ones selected, because
        every consumer — the exit dump, the examples, a human reading a tail
        — wants them in the order they happened.
        """

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
    def task_events_since(self, after_id: int) -> TaskDelta: ...

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
        try:
            with self._lock:
                if path != ":memory:":
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._reject_a_foreign_schema()
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
        except BaseException:
            # Failing here must not leave the connection open and unreachable
            # — the same rule `init()` follows for a store it created. From
            # 3.13 an unclosed connection also emits a ResourceWarning at
            # collection, which this suite turns into an error, so a leak
            # here fails an unrelated test later.
            self._conn.close()
            raise

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
        self,
        n: int | None = DEFAULT_RECENT_LIMIT,
        since: float | None = None,
    ) -> Sequence[StoredRecord]:
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
            # nosemgrep: every fragment concatenated into `sql` above is a
            # string literal; all values go through `?` placeholders.
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
            # nosemgrep: every fragment concatenated into `sql` above is a
            # string literal; all values go through `?` placeholders.
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
            # nosemgrep: every fragment concatenated into `sql` above is a
            # string literal; all values go through `?` placeholders.
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

        Task-event rows are excluded from the counts: the tracking API knows
        its own exact numbers and gets its own bars, so counting its rows here
        too would draw a source-location bar beside every named one.

        They are excluded by *grouping*, not by a `WHERE` clause, and that
        matters. Filtering them out of the range would take `MAX(id)` over the
        surviving rows only, so a delta whose newest rows are all task events
        would leave the watermark behind them — and every later poll would
        rescan a range that only grows. Grouping on the predicate keeps the
        watermark over the whole range while still dropping the counts.
        Measured at a 5,000-row delta over 300k rows: 1.8ms against 2.2ms,
        same query plan.

        `NOT INDEXED` is load-bearing, not leftover debugging. Left to itself
        SQLite serves the GROUP BY from `idx_records_source` as a covering
        index — no sort, but a scan of every row in the store, which is the
        cost this method exists to avoid. `NOT INDEXED` rules that out while
        still allowing the INTEGER PRIMARY KEY, so the plan becomes a rowid
        range seek plus a sort of the delta. Measured over 1M rows: 32ms
        against 0.5ms.

        Grouping by worker as well as by source costs a wider sort key and one
        group per (source, worker) pair instead of per source. Measured at a
        5,000-row delta over 1M rows with 20 sources across 8 threads: 5.2ms
        against 3.3ms, 160 rows against 20. Still linear in what arrived
        rather than in what the store holds, which is the property that
        matters; the multiplier is how many workers actually touch a line,
        which for real code is small.
        """
        sql = (
            "SELECT pathname, lineno, func_name, task_event IS NULL AS is_plain, "
            "process, thread, asyncio_task_id, "
            "COUNT(*) AS cnt, MAX(id) AS max_id, "
            "MIN(created) AS first_at, MAX(created) AS last_at "
            "FROM records NOT INDEXED WHERE id > ? "
            "GROUP BY pathname, lineno, func_name, is_plain, "
            "process, thread, asyncio_task_id"
        )
        with self._lock:
            rows = self._conn.execute(sql, (after_id,)).fetchall()
        # Grouping by worker as well as by source splits each source into one
        # row per worker, so the per-source figures are refolded here. That is
        # cheaper than it looks — the extra groups are bounded by how many
        # workers touch a line, and the whole delta is already in memory.
        counts: dict[SourceKey, int] = {}
        first_at: dict[SourceKey, float] = {}
        last_at: dict[SourceKey, float] = {}
        workers: dict[SourceKey, set[WorkerKey]] = {}
        for row in rows:
            if not row["is_plain"]:
                continue
            key = SourceKey(row["pathname"], row["lineno"], row["func_name"])
            counts[key] = counts.get(key, 0) + row["cnt"]
            first_at[key] = min(first_at.get(key, row["first_at"]), row["first_at"])
            last_at[key] = max(last_at.get(key, row["last_at"]), row["last_at"])
            workers.setdefault(key, set()).add(
                WorkerKey(row["process"], row["thread"], row["asyncio_task_id"])
            )
        # No new rows leaves the watermark where it was; never move it back.
        return SourceDelta(
            counts=counts,
            last_id=max((r["max_id"] for r in rows), default=after_id),
            first_at=first_at,
            last_at=last_at,
            workers={k: frozenset(v) for k, v in workers.items()},
        )

    def task_events_since(self, after_id: int) -> TaskDelta:
        """The tracking API's rows appended after `after_id`, oldest first.

        The same watermark contract as `count_by_source_since()`: costs what
        arrived, not what the store holds. Rows rather than aggregates,
        because a task bar is the *latest* state per task rather than a tally
        — and there are few of them, since progress ticks are sampled.

        No index on `task_event`, deliberately: the rowid range already bounds
        the scan to the delta, and an index would cost every write to speed up
        a filter over rows we are reading anyway. Measured below.
        """
        sql = (
            "SELECT id, task_id, parent_task_id, task_label, task_event, "
            "progress_current, progress_total "
            "FROM records WHERE id > ? AND id <= ? AND task_event IS NOT NULL "
            "ORDER BY id"
        )
        # Both statements under one lock, and the ceiling read first: `append()`
        # takes the same lock, so no row can land between them. Two separate
        # acquisitions would let a task event arrive after the rows were read
        # but before the watermark was taken, and that event would be skipped
        # for good.
        with self._lock:
            top = self._conn.execute(
                "SELECT MAX(id) AS max_id FROM records WHERE id > ?", (after_id,)
            ).fetchone()["max_id"]
            if top is None:
                return TaskDelta((), after_id)
            rows = self._conn.execute(sql, (after_id, top)).fetchall()
        events = tuple(
            TaskEventRow(
                task_id=r["task_id"],
                parent_task_id=r["parent_task_id"],
                label=r["task_label"],
                event=r["task_event"],
                current=r["progress_current"],
                total=r["progress_total"],
            )
            for r in rows
        )
        return TaskDelta(events, top)

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
