"""Pluggable record storage.

`RecordStore` is the interface analysis and rendering talk to; they never
touch a backend's native representation directly. `SQLiteRecordStore` is the
zero-dependency default. Backends share one parametrized test suite
(tests/conftest.py `store` fixture) so a future DuckDB backend slots in
without changing test code.
"""

from __future__ import annotations

import abc
import contextlib
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from operator import attrgetter
from typing import NamedTuple, override

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
# The parity tests pin `_COLUMNS` against the real table.
_INSERT_SQL = (
    "INSERT INTO records ("
    "logger_name, level_name, level_no, msg, message, pathname, filename, "
    "module, func_name, lineno, created, thread, thread_name, process, "
    "process_name, exc_text, stack_text, asyncio_task_name, asyncio_task_id, "
    "task_id, parent_task_id, task_label, task_event, progress_current, "
    "progress_total, template_id"
    ") VALUES ("
    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
    ")"
)


class RecordStore(abc.ABC):
    """What every reader talks to, and the contract a backend implements.

    The methods below are the specification; `SQLiteRecordStore` is one
    implementation of them and is not it. Where that class's docstrings say
    anything, they say what SQLite does about a requirement stated here —
    never what a store must do.

    Concurrent calls from several threads must work, and that is a
    requirement of the interface rather than a convenience of the default
    backend: in an ordinary session the flush pump appends from one thread
    while the display's redraw timer reads from another, and `shutdown()`
    appends and closes from whichever thread called it. `SQLiteRecordStore`
    meets it with one internal lock serializing every statement; another
    backend may meet it some other way, but not skip it.
    """

    @abc.abstractmethod
    def append(self, rows: Sequence[LogRecordRow]) -> None:
        """Take a batch of captured rows and keep every one of them.

        The only write path, and the lossless half of Principle 6: a record
        that reached here and did not reach the store is a bug rather than a
        trade-off. Rows arrive in the order they were logged and the store
        assigns each an ascending id — every watermark below is one of those
        ids, so that ordering is contract and not incident.

        An empty `rows` does nothing, which is the ordinary case: the pump
        ticks on a timer whether or not anything was logged.

        **All-or-nothing per batch**, and a backend owes this rather than
        merely offering it: a raising `append()` must leave the store exactly
        as it found it. `LumberjackHandler.restore()` puts a failed batch back
        for the next flush to retry, so a partial write would be replayed on
        top of itself and duplicate every row the first attempt did manage.
        """

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
        — wants them in the order they happened. That is also why there is
        deliberately no separate `tail()`: the last `n` in the order they
        happened is already what a tail means, and it is what `atexit`'s
        diagnostic dump reads.
        """

    @abc.abstractmethod
    def count_by_template(
        self, window_seconds: float | None = None
    ) -> Mapping[int | None, int]:
        """How many records carry each template id, including none at all.

        Records with no template group under a `None` key, which today is
        every captured record: `template_id` is a reserved column and nothing
        writes it yet. `window_seconds` narrows the tally to records created
        within that many seconds of now; without it it covers the whole
        store.
        """

    @abc.abstractmethod
    def count_by_source(
        self, window_seconds: float | None = None
    ) -> Mapping[SourceKey, int]:
        """How many records each source location produced.

        Counts everything, the tracking API's own rows included — unlike
        `count_by_source_since()`, which drops them. `window_seconds` narrows
        it as in `count_by_template()`. There is no watermark, so the cost is
        the whole store every time: this answers a one-off question, never a
        redraw loop's.
        """

    @abc.abstractmethod
    def count_by_source_since(self, after_id: int) -> SourceDelta:
        """Per-source counts for the rows appended after `after_id`.

        The one method anything on a timer should call: it groups only what
        is past the watermark, so a poll costs what arrived rather than what
        the store holds. `SourceDelta.last_id` is the next call's `after_id`
        and spans every row in the range — including the ones the counts drop
        — so nothing is ever rescanned, and a delta with no new rows returns
        `after_id` unchanged rather than rewinding.

        Task-event rows are excluded from the counts, because the tracking
        API knows its own exact numbers and draws its own bars; they still
        move the watermark. Grouping is by worker
        `(process, thread, asyncio_task_id)` as well as by source, and
        `SourceDelta.workers` reports which workers each line was seen on —
        containment analysis needs a shared worker before it may call one
        loop nested inside another.
        """

    @abc.abstractmethod
    def task_events_since(self, after_id: int) -> TaskDelta:
        """The tracking API's rows appended after `after_id`, oldest first.

        Rows rather than aggregates: a task bar is the latest state per task
        rather than a tally, and there are few of them because progress ticks
        are sampled. The watermark works as `count_by_source_since()`'s does
        — `last_id` spans the whole range, ordinary records included, so they
        are never rescanned.

        The rows and `last_id` have to describe one snapshot. Read
        separately, an event landing between them sits below the new
        watermark and is skipped for good.
        """

    @abc.abstractmethod
    def evict(
        self, *, before: float | None = None, keep_last: int | None = None
    ) -> int:
        """Drop old records, by age or by count, and say how many went.

        Exactly one of `before` and `keep_last`, or `ValueError` — neither
        absence is a default worth guessing at. `before` is a `time.time()`
        timestamp and is exclusive. `keep_last` is a floor rather than a
        target: asking to keep more rows than exist deletes nothing, and zero
        clears the store rather than meaning "no limit".
        """

    @abc.abstractmethod
    def templates(self) -> Sequence[int]:
        """The distinct template ids present, NULL excluded.

        Empty today, since nothing writes `template_id` — the records
        `count_by_template()` files under `None` have no id to return here.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release the backend's resources; the store is finished afterwards.

        Not a flush and not a reset: nothing is promised about a closed store
        beyond its being closed, and a backend is free to raise out of any
        later query rather than answer one. `shutdown()` closes only a store
        lumberjack created — one handed to `init()` stays open and remains the
        caller's to close.
        """


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

    @override
    def append(self, rows: Sequence[LogRecordRow]) -> None:
        """One `executemany` and one commit per call: the batch is the unit.

        Rolled back on failure, which is what makes that sentence true in the
        case it matters. `executemany` can raise partway through, leaving the
        rows it did insert in an open transaction — a later `commit()` from
        the next batch would then write them, and the retry in
        `LumberjackHandler.restore()` would write them a second time. Explicit
        rollback is what the interface's all-or-nothing promise costs.
        """
        if not rows:
            return
        values = list(map(_GET_COLUMNS, rows))
        with self._lock:
            try:
                self._conn.executemany(_INSERT_SQL, values)
                self._conn.commit()
            except Exception:
                # A closed connection raises here too, and there is nothing
                # to roll back on one — the original failure is what the
                # caller needs to see.
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                raise

    def _row_to_stored(self, row: sqlite3.Row) -> StoredRecord:
        kwargs = {col: row[col] for col in _COLUMNS}
        return StoredRecord(id=row["id"], **kwargs)

    @override
    def recent(
        self,
        n: int | None = DEFAULT_RECENT_LIMIT,
        since: float | None = None,
    ) -> Sequence[StoredRecord]:
        """Selected newest-first by rowid under the `LIMIT`, reversed here."""
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
            # Every fragment concatenated into `sql` above is a string
            # literal; all values go through `?` placeholders.
            rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [self._row_to_stored(r) for r in reversed(rows)]

    @override
    def count_by_template(
        self, window_seconds: float | None = None
    ) -> Mapping[int | None, int]:
        """Grouped off `idx_records_template_id`, so no temporary b-tree.

        Unwindowed the index covers the query outright; `window_seconds` adds
        `created`, which that index does not carry, so the rows are visited.
        The grouping stays in index order either way.
        """
        sql = "SELECT template_id, COUNT(*) AS cnt FROM records"
        params: list[object] = []
        if window_seconds is not None:
            sql += " WHERE created >= ?"
            params.append(time.time() - window_seconds)
        sql += " GROUP BY template_id"
        with self._lock:
            # Every fragment concatenated into `sql` above is a string
            # literal; all values go through `?` placeholders.
            rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return {r["template_id"]: r["cnt"] for r in rows}

    @override
    def count_by_source(
        self, window_seconds: float | None = None
    ) -> Mapping[SourceKey, int]:
        """Grouped off `idx_records_source`, in index order and unsorted.

        That is the same plan `count_by_source_since()` refuses with
        `NOT INDEXED`, and the right one here: with no watermark every row is
        in range anyway, so there is nothing to seek past.
        """
        sql = "SELECT pathname, lineno, func_name, COUNT(*) AS cnt FROM records"
        params: list[object] = []
        if window_seconds is not None:
            sql += " WHERE created >= ?"
            params.append(time.time() - window_seconds)
        sql += " GROUP BY pathname, lineno, func_name"
        with self._lock:
            # Every fragment concatenated into `sql` above is a string
            # literal; all values go through `?` placeholders.
            rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return {
            SourceKey(r["pathname"], r["lineno"], r["func_name"]): r["cnt"]
            for r in rows
        }

    @override
    def count_by_source_since(self, after_id: int) -> SourceDelta:
        """A rowid-range delta, grouped in one pass.

        Task rows are dropped by *grouping* on the predicate, not by a
        `WHERE` clause, and that matters. Filtering them out of the range
        would take `MAX(id)` over the surviving rows only, so a delta whose
        newest rows are all task events would leave the watermark behind them
        — and every later poll would rescan a range that only grows. Grouping
        on the predicate keeps the watermark over the whole range while still
        dropping the counts. Measured at a 5,000-row delta over 300k rows:
        1.8ms against 2.2ms, same query plan.

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

    @override
    def task_events_since(self, after_id: int) -> TaskDelta:
        """No index on `task_event`, deliberately.

        The rowid range already bounds the scan to the delta, so an index
        would cost every write to speed up a filter over rows this is reading
        anyway.
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

    @override
    def evict(
        self, *, before: float | None = None, keep_last: int | None = None
    ) -> int:
        """Bounded eviction deletes over an index range: `created`, or the rowid.

        `keep_last=0` is neither — an unqualified `DELETE FROM records`, with
        no predicate and no index to range over.
        """
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

    @override
    def templates(self) -> Sequence[int]:
        """One `SELECT DISTINCT`, off `idx_records_template_id`.

        The index covers it and the NULL filter is a range bound on that
        index, so nothing touches the table.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT template_id FROM records WHERE template_id IS NOT NULL"
            ).fetchall()
        return [r["template_id"] for r in rows]

    @override
    def close(self) -> None:
        """One `close()` on the `sqlite3` connection, under the lock.

        A second `close()` is a no-op, but every later *query* raises
        `sqlite3.ProgrammingError` — which is what `shutdown()` leaves behind
        for a store it owned.
        """
        with self._lock:
            self._conn.close()
