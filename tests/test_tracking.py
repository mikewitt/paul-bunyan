"""The tracking API, logging side only — no `rich`, no OTel, so this runs on
a bare install.

Most tests assert on rows in the store, because that is the observable
contract: what a user of `init()` can actually query afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator

import pytest

import lumberjack
from lumberjack.schema import StoredRecord
from lumberjack.store import SQLiteRecordStore
from lumberjack.tracking import TASK_LOGGER_NAME, TICK_INTERVAL


class _Session:
    """A live `init()` plus a way to read back what it stored.

    The pump is off and `read()` flushes explicitly, so nothing here waits on
    a timer. The store is caller-supplied, so `shutdown()` leaves it open.
    """

    def __init__(self, store: SQLiteRecordStore) -> None:
        self._store = store

    def read(self) -> list[StoredRecord]:
        lumberjack.flush()
        return [r for r in self._store.recent() if r.task_event]

    def events(self) -> list[tuple[str | None, str | None]]:
        return [(r.task_event, r.task_label) for r in self.read()]


@pytest.fixture
def session() -> Iterator[_Session]:
    store = SQLiteRecordStore(":memory:")
    lumberjack.init(store=store, output_mode="plain", flush_interval=0)
    try:
        yield _Session(store)
    finally:
        lumberjack.shutdown()
        store.close()


# --- the inert case: no init() ---------------------------------------------


def test_a_task_without_init_emits_nothing(capsys):
    """The rung-1 promise from the other side: a library can instrument freely
    and a host application that never called `init()` sees no output.

    The root logger is configured with a handler first, on purpose. That is
    the configuration under which an always-emitting `task()` *would* print,
    since lumberjack's own handler reaches records by sitting on the root
    logger. Asserting against an unconfigured root would pass vacuously.
    """
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        with lumberjack.task("invisible") as t:
            t.advance()
            t.set_progress(5, total=10)
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_task_without_init_still_hands_back_a_working_handle():
    """Inert is not broken: the object still counts, still nests, still exits
    cleanly. Only the emission is skipped."""
    with lumberjack.task("invisible", total=3) as t:
        t.advance()
        child = t.subtask("nested")
        child.end()
    assert child.parent_task_id == t.task_id


# --- emission under init() --------------------------------------------------


def test_a_task_writes_a_start_and_an_end_row(session):
    with lumberjack.task("reindex"):
        pass
    assert session.events() == [("start", "reindex"), ("end", "reindex")]


def test_the_start_row_is_written_before_the_with_is_entered(session):
    """A handle exists from the moment it is created, entered or not — which
    is also what lets `track()` open one eagerly and hand back a generator."""
    handle = lumberjack.task("eager")
    try:
        assert session.events() == [("start", "eager")]
    finally:
        handle.end()


def test_progress_columns_carry_the_absolute_count(session):
    with lumberjack.task("reindex", total=100) as t:
        t.set_progress(40)
    rows = session.read()
    assert [(r.progress_current, r.progress_total) for r in rows] == [
        (0, 100),
        (40, 100),
        (40, 100),
    ]


def test_the_end_row_carries_the_final_count(session):
    """`end` doubles as the unsampled final tick, which is why sampled
    `advance()` loses nothing."""
    with lumberjack.task("reindex", total=3) as t:
        for _ in range(3):
            t.advance()
    end = session.read()[-1]
    assert (end.task_event, end.progress_current) == ("end", 3)


def test_task_rows_go_to_the_dedicated_logger(session):
    """One fixed name, so an application layering with
    `replace_handlers=False` can silence or route all of it at once."""
    with lumberjack.task("reindex"):
        pass
    assert {r.logger_name for r in session.read()} == {TASK_LOGGER_NAME}


def test_task_rows_are_attributed_to_the_call_site_not_to_lumberjack(session):
    """Source-location grouping is what today's bar keys on, so attributing
    to `tracking.py` would collapse every task in the program into one bar."""
    with lumberjack.task("reindex") as t:
        t.set_progress(1)
    for row in session.read():
        assert row.filename == "test_tracking.py"
        assert row.func_name == (
            "test_task_rows_are_attributed_to_the_call_site_not_to_lumberjack"
        )


def test_subtask_is_attributed_to_its_own_call_site(session):
    """`.subtask()` captures its own frame rather than delegating to `task()`;
    delegating would attribute every subtask to one line of this package."""
    with lumberjack.task("outer") as outer:
        outer.subtask("inner").end()
    inner = [r for r in session.read() if r.task_label == "inner"]
    assert inner and all(r.filename == "test_tracking.py" for r in inner)


def test_the_message_text_is_the_documented_contract(session):
    """These strings are what the plain and JSON-lines renderers print."""
    with lumberjack.task("reindex", total=10) as t:
        t.set_progress(4)
    assert [r.message for r in session.read()] == [
        "task start: reindex",
        "task progress: reindex 4/10",
        "task end: reindex",
    ]


def test_progress_without_a_total_reads_as_a_bare_count(session):
    with lumberjack.task("scan") as t:
        t.set_progress(7)
    assert session.read()[1].message == "task progress: scan 7"


def test_a_failing_task_records_the_exception_at_error(session):
    with pytest.raises(ValueError):
        with lumberjack.task("doomed"):
            raise ValueError("boom")
    end = session.read()[-1]
    assert end.level_name == "ERROR"
    assert end.task_event == "end"
    assert "task failed: doomed" in end.message
    assert "boom" in end.message


def test_a_level_the_logger_filters_out_emits_nothing(session):
    """`isEnabledFor` gates the record, as it would for any other log call."""
    logging.getLogger(TASK_LOGGER_NAME).setLevel(logging.CRITICAL)
    try:
        with lumberjack.task("quiet"):
            pass
        assert session.events() == []
    finally:
        logging.getLogger(TASK_LOGGER_NAME).setLevel(logging.NOTSET)


def test_ticks_are_sampled_rather_than_one_row_per_call(session):
    """The shipping constraint, not an optimisation: a record per `advance()`
    outruns the write buffer's drain rate and trips lumberjack's own
    dropped-records warning, and the write-through plain renderer would print
    a line per item."""
    with lumberjack.task("hot loop", total=50_000) as t:
        for _ in range(50_000):
            t.advance()
    rows = session.read()
    elapsed_ticks = 1 + 1 / TICK_INTERVAL  # generous: the loop is far faster
    assert len(rows) <= elapsed_ticks + 2, f"{len(rows)} rows for 50k advances"
    assert rows[-1].progress_current == 50_000, "sampling must not lose count"
    handler = lumberjack.current_handler()
    assert handler is not None and handler.dropped == 0


def test_layering_sends_task_events_to_the_application_too(capsys):
    """Documented, and a surprise worth pinning: under `replace_handlers=False`
    the application asked to layer, so it sees task events as well."""
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    store = SQLiteRecordStore(":memory:")
    lumberjack.init(
        store=store,
        output_mode="plain",
        flush_interval=0,
        replace_handlers=False,
    )
    try:
        with lumberjack.task("shared"):
            pass
        lumberjack.flush()
        assert [r.task_event for r in store.recent()] == ["start", "end"]
    finally:
        lumberjack.shutdown()
        root.removeHandler(handler)
        store.close()
    assert "task start: shared" in capsys.readouterr().err


# --- hierarchy --------------------------------------------------------------


def test_a_nested_task_records_its_parent(session):
    with lumberjack.task("outer") as outer:
        with lumberjack.task("inner") as inner:
            assert inner.parent_task_id == outer.task_id
    inner_rows = [r for r in session.read() if r.task_label == "inner"]
    assert {r.parent_task_id for r in inner_rows} == {outer.task_id}


def test_a_bare_handle_never_becomes_a_parent():
    """Only `__enter__` establishes ambient parentage, which is what makes an
    out-of-order contextvar reset structurally impossible."""
    outer = lumberjack.task("never entered")
    try:
        with lumberjack.task("sibling") as sibling:
            assert sibling.parent_task_id is None
    finally:
        outer.end()


def test_subtask_parents_explicitly_across_a_thread():
    """`contextvars` do not propagate into a bare `threading.Thread`, so a
    worker has to be handed its handle. This is the shape the demo uses."""
    seen: list[int | None] = []

    with lumberjack.task("pool") as parent:

        def worker() -> None:
            with parent.subtask("worker") as child:
                seen.append(child.parent_task_id)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    assert seen == [parent.task_id]


def test_ambient_parentage_follows_an_asyncio_task():
    """Unlike threads, contextvars *do* propagate into asyncio tasks, so a
    plain `task()` inside one nests without being handed anything."""
    seen: list[int | None] = []

    async def child() -> None:
        with lumberjack.task("child") as c:
            seen.append(c.parent_task_id)

    async def main() -> None:
        with lumberjack.task("parent") as parent:
            await asyncio.gather(child())
            seen.append(parent.task_id)

    asyncio.run(main())
    assert seen[0] == seen[1]


def test_nesting_unwinds_in_order():
    with lumberjack.task("a") as a:
        with lumberjack.task("b") as b:
            assert b.parent_task_id == a.task_id
        with lumberjack.task("c") as c:
            assert c.parent_task_id == a.task_id
    with lumberjack.task("d") as d:
        assert d.parent_task_id is None


# --- lifecycle --------------------------------------------------------------


def test_end_is_idempotent():
    t = lumberjack.task("once")
    t.end()
    t.end()  # must not raise


def test_a_handle_cannot_be_entered_twice():
    t = lumberjack.task("once")
    with t:
        with pytest.raises(RuntimeError, match="cannot be entered twice"):
            t.__enter__()


def test_a_finished_handle_cannot_be_re_entered():
    t = lumberjack.task("once")
    with t:
        pass
    with pytest.raises(RuntimeError, match="cannot be entered twice"):
        t.__enter__()


def test_advancing_after_the_end_is_a_noop():
    t = lumberjack.task("done")
    t.end()
    t.advance()  # must not raise


def test_an_exception_propagates_out_of_the_with():
    with pytest.raises(ValueError, match="boom"):
        with lumberjack.task("doomed"):
            raise ValueError("boom")


def test_exiting_in_a_different_context_does_not_mask_the_real_exception():
    """`ContextVar.reset()` raises when the token came from another Context,
    and raising out of `__exit__`'s `finally` would replace whatever the user
    was already propagating. Only reachable by driving the protocol by hand
    across an asyncio task boundary — verified that generators, sync and
    async, do not hit it."""

    async def main() -> None:
        handle = lumberjack.task("crossing")

        async def enter() -> None:
            handle.__enter__()

        await asyncio.create_task(enter())
        with pytest.raises(ValueError, match="the user's error"):
            try:
                raise ValueError("the user's error")
            except ValueError as exc:
                handle.__exit__(type(exc), exc, exc.__traceback__)
                raise

    asyncio.run(main())
