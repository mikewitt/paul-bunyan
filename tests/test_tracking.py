"""The tracking API, logging side only — no `rich`, no OTel, so this runs on
a bare install.

Most tests assert on rows in the store, because that is the observable
contract: what a user of `init()` can actually query afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading

import pytest

import lumberjack
from lumberjack import tracking
from lumberjack.tracking import TASK_LOGGER_NAME, TICK_INTERVAL

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


def test_advance_takes_a_step_size(session):
    with lumberjack.task("batched", total=100) as t:
        t.advance(25)
    assert session.read()[-1].progress_current == 25


def test_set_progress_can_revise_the_total(session):
    with lumberjack.task("resized") as t:
        t.set_progress(3, total=30)
    end = session.read()[-1]
    assert (end.progress_current, end.progress_total) == (3, 30)


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
        "task end: reindex 4/10",
    ]


def test_the_end_message_carries_the_count_for_plain_text_readers(session):
    """`PlainTextRenderer` prints `message` and nothing else, so a non-TTY
    user gets no bar and no columns — the end line is the only place the
    final count can reach them, and it is what justifies sampling ticks."""
    with lumberjack.task("scan") as t:
        t.set_progress(9)
    assert session.read()[-1].message == "task end: scan 9"


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


def test_the_failure_row_carries_a_traceback(session):
    """The one record type with an exception in hand should fill the column
    the schema keeps for one."""
    with pytest.raises(ValueError):
        with lumberjack.task("doomed"):
            raise ValueError("boom")
    end = session.read()[-1]
    assert end.exc_text is not None
    assert "ValueError: boom" in end.exc_text


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


def test_layering_sends_task_events_to_the_application_too(capsys, make_session):
    """Documented, and a surprise worth pinning: under `replace_handlers=False`
    the application asked to layer, so it sees task events as well."""
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        with make_session(replace_handlers=False) as live:
            with lumberjack.task("shared"):
                pass
            assert [event for event, _ in live.events()] == ["start", "end"]
    finally:
        root.removeHandler(handler)
    assert "task start: shared" in capsys.readouterr().err


# --- hierarchy --------------------------------------------------------------


def test_a_nested_task_records_its_parent(session):
    with lumberjack.task("outer") as outer:
        with lumberjack.task("inner") as inner:
            assert inner.parent_task_id == outer.task_id
    inner_rows = [r for r in session.read() if r.task_label == "inner"]
    assert {r.parent_task_id for r in inner_rows} == {outer.task_id}


def test_a_bare_handle_never_becomes_a_parent(session):
    """Only `__enter__` establishes ambient parentage, so a handle used bare
    holds no token and cannot be the one whose reset misfires. That narrows
    the out-of-order-reset problem; it does not remove it — see the
    interleaved-generator tests below."""
    outer = lumberjack.task("never entered")
    try:
        with lumberjack.task("sibling") as sibling:
            assert sibling.parent_task_id is None
        assert all(r.parent_task_id is None for r in session.read())
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


def test_a_subtask_takes_its_own_total(session):
    with lumberjack.task("parent") as parent:
        with parent.subtask("child", total=5) as child:
            child.advance()
    rows = [r for r in session.read() if r.task_label == "child"]
    assert rows[-1].progress_total == 5


# --- lifecycle --------------------------------------------------------------


def test_end_is_idempotent(session):
    """Idempotence means "writes no second end row", which is only visible
    with a session — without one nothing is written either way."""
    t = lumberjack.task("once")
    t.end()
    t.end()
    assert session.events() == [("start", "once"), ("end", "once")]


def test_a_handle_cannot_be_entered_twice(session):
    t = lumberjack.task("once")
    with t:
        with pytest.raises(RuntimeError, match="cannot be entered twice"):
            t.__enter__()


def test_a_finished_handle_cannot_be_re_entered(session):
    t = lumberjack.task("once")
    with t:
        pass
    with pytest.raises(RuntimeError, match="cannot be entered twice"):
        t.__enter__()


def test_advancing_after_the_end_is_a_noop(session):
    t = lumberjack.task("done")
    t.end()
    t.advance()
    assert session.events() == [("start", "done"), ("end", "done")]


def test_an_exception_propagates_out_of_the_with(session):
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


def test_exiting_without_entering_is_harmless(session):
    """Reachable for real: `contextlib.ExitStack.push()` registers a handle's
    `__exit__` without ever calling `__enter__`."""
    handle = lumberjack.task("pushed")
    with contextlib.ExitStack() as stack:
        stack.push(handle)
    assert session.events() == [("start", "pushed"), ("end", "pushed")]
    with lumberjack.task("after") as after:
        assert after.parent_task_id is None


# --- track() ----------------------------------------------------------------


def test_track_yields_every_item(session):
    assert list(lumberjack.track([1, 2, 3], name="items")) == [1, 2, 3]


def test_track_takes_its_total_from_len(session):
    list(lumberjack.track(["a", "b", "c"], name="items"))
    assert {r.progress_total for r in session.read()} == {3}


def test_track_never_consumes_a_generator_to_find_a_total(session):
    """Measuring the length of a stream would defeat streaming it."""
    consumed: list[int] = []

    def stream():
        for i in range(3):
            consumed.append(i)
            yield i

    generator = lumberjack.track(stream(), name="stream")
    assert consumed == [], "the source was drained before the first next()"
    assert next(generator) == 0
    assert consumed == [0]
    generator.close()
    assert {r.progress_total for r in session.read()} == {None}


def test_track_starts_the_task_before_the_first_next(session):
    """A generator function's body would not run until the first `next()`,
    which is why `track()` is a plain function returning an inner generator."""
    lumberjack.track([1, 2, 3], name="eager")
    assert session.events() == [("start", "eager")]


def test_track_is_attributed_to_its_own_call_site(session):
    """`_origin=` forwarding: without it the task lands on `tracking.py` and
    every `track()` in the program collapses into one bar."""
    list(lumberjack.track([1], name="items"))
    for row in session.read():
        assert row.filename == "test_tracking.py"
        assert row.func_name == "test_track_is_attributed_to_its_own_call_site"


def test_track_ends_the_task_on_an_early_break(session):
    """`break` closes the generator with `GeneratorExit`, and the `finally`
    has to end the task on that path too."""
    for item in lumberjack.track(range(100), name="aborted"):
        if item == 2:
            break
    assert session.events()[-1] == ("end", "aborted")


def test_closing_a_generator_early_is_not_a_task_failure(session):
    """`GeneratorExit` is control flow, not an error: it is what a user's
    generator receives when the consumer stops early. `track()`'s own early
    `break` already records a clean end, and a bare `with` inside a generator
    must agree — otherwise an ordinary `break` puts a traceback on the ERROR
    channel, which is the one place a user's attention is guaranteed."""

    def stage():
        with lumberjack.task("stage"):
            yield from range(100)

    for item in stage():
        if item == 2:
            break

    end = session.read()[-1]
    assert end.task_event == "end"
    assert end.level_name == "INFO"
    assert end.exc_text is None
    assert "failed" not in end.message


def test_track_ends_the_task_when_the_body_raises(session):
    with pytest.raises(ValueError):
        for _ in lumberjack.track(range(100), name="doomed"):
            raise ValueError("boom")
    assert session.events()[-1] == ("end", "doomed")


def test_track_counts_every_item_despite_sampling(session):
    """Sampling is lossless because `end` carries the final absolute count."""
    count = 20_000
    for _ in lumberjack.track(range(count), name="hot"):
        pass
    rows = session.read()
    assert rows[-1].progress_current == count
    assert len(rows) < 20, f"{len(rows)} rows for {count} items — not sampled"
    handler = lumberjack.current_handler()
    assert handler is not None and handler.dropped == 0


def test_track_without_a_name_labels_by_type(session):
    list(lumberjack.track([1, 2]))
    assert {r.task_label for r in session.read()} == {"list"}


def test_track_nests_under_an_open_task(session):
    with lumberjack.task("outer") as outer:
        list(lumberjack.track([1], name="inner"))
    inner = [r for r in session.read() if r.task_label == "inner"]
    assert inner and {r.parent_task_id for r in inner} == {outer.task_id}


def test_track_without_init_emits_nothing(capsys):
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        assert list(lumberjack.track([1, 2], name="quiet")) == [1, 2]
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


# --- the contextvar chain under interleaving --------------------------------


def test_interleaved_generators_do_not_leave_a_dead_task_ambient(session):
    """`with` is LIFO within one frame, but two suspended generators each
    holding one interleave freely — and `ContextVar.reset()` does *not* raise
    for an out-of-order token from the same Context. It silently writes the
    old value back, which used to leave a *finished* handle ambient and parent
    every later task in the thread to a dead one. `track()` returns
    generators, so this is reachable rather than theoretical."""

    def held(name):
        with lumberjack.task(name):
            yield

    first, second = held("first"), held("second")
    next(first)
    next(second)
    first.close()  # out of order
    second.close()

    with lumberjack.task("after") as after:
        assert after.parent_task_id is None


def test_an_earlier_exit_does_not_evict_a_still_open_later_task(session):
    """The other half of the same fix. When the *first*-opened generator
    closes first, a blind `reset()` writes back the value from before it —
    evicting the second generator's task, which is still running."""

    def held(name, seen):
        with lumberjack.task(name):
            yield
            with lumberjack.task(f"{name}-child") as child:
                seen.append(child.parent_task_id)

    seen: list[int | None] = []
    first, second = held("first", seen), held("second", seen)
    next(first)
    next(second)
    first.close()  # the earlier one goes first
    next(second, None)  # `second` is still open and must still be ambient

    assert seen and seen[0] is not None, "the still-open task stopped parenting"


def test_a_finished_task_is_never_a_parent(session):
    """Whatever the token bookkeeping does, the ambient lookup walks past
    handles that have already ended."""
    with lumberjack.task("outer") as outer:
        with lumberjack.task("middle") as middle:
            middle.end()
            with lumberjack.task("child") as child:
                assert child.parent_task_id == outer.task_id


def test_a_cross_context_exit_does_not_orphan_an_enclosing_task(session):
    """`ContextVar.reset()` raises when the token came from another Context.
    Reachable by entering in the outer context and exiting inside an asyncio
    task, which runs on a *copy* — so the handle is ambient there but the
    token is foreign. Nothing may escape `__exit__`'s `finally`, and the
    enclosing task must survive untouched."""
    seen: list[int | None] = []

    async def main() -> None:
        with lumberjack.task("outer"):
            crossing = lumberjack.task("crossing")
            crossing.__enter__()

            async def leave() -> None:
                crossing.__exit__(None, None, None)

            await asyncio.create_task(leave())
            with lumberjack.task("sibling") as sibling:
                seen.append(sibling.parent_task_id)

    asyncio.run(main())
    assert seen and seen[0] is not None, "the enclosing task was orphaned"


def test_a_handle_entered_inside_another_task_walks_back_to_it(session):
    """The ambient walk follows where a handle was *entered*, not where it was
    created. A handle built before the enclosing task has no creation-time
    link to it, so walking `_parent` would step straight past a `with` block
    that is still open and orphan the next task."""

    def entering(handle):
        with handle:
            yield

    def running(name):
        with lumberjack.task(name):
            yield

    outside = lumberjack.task("built outside")
    with lumberjack.task("enclosing") as enclosing:
        first = entering(outside)
        second = running("inner")
        next(first)  # enters `outside` while `enclosing` is ambient
        next(second)  # enters `inner`, whose parent is `outside`
        first.close()  # out of order: reset skipped, `outside` ends
        second.close()  # restores the ended `outside` as ambient
        with lumberjack.task("after") as after:
            assert after.parent_task_id == enclosing.task_id


# --- concurrency ------------------------------------------------------------


def test_concurrent_advances_are_not_lost(session):
    """`self._current += n` is a read-modify-write, so the handle's lock is
    what makes this exact.

    Honest caveat: on a GIL build this passes with the lock removed too —
    measured over several runs at `sys.setswitchinterval(1e-9)`, the update
    never tore. The lock is there for free-threaded builds, which CI does not
    currently run — every matrix leg is a GIL build. This test pins the
    contract; it cannot demonstrate the mechanism."""
    threads, per_thread = 8, 20_000

    with lumberjack.task("counted") as t:
        workers = [
            threading.Thread(target=lambda: [t.advance() for _ in range(per_thread)])
            for _ in range(threads)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    assert session.read()[-1].progress_current == threads * per_thread


def test_the_end_row_is_last_when_end_races_in_flight_advances(session, monkeypatch):
    """Emission happens under the lock, so an `update` in flight on another
    thread cannot land after the `end` row. Readers treat `end` as terminal,
    and the whole defence of sampling is that it carries the final count.

    `end()` has to be called *while* workers are advancing — joining them
    first makes the test unfalsifiable. The barrier guarantees every worker
    is inside the loop, and a zero tick interval makes every advance emit,
    so the window is as wide as it gets.
    """
    monkeypatch.setattr(tracking, "TICK_INTERVAL", 0.0)
    workers_n, rounds = 4, 20
    handles = []

    for _ in range(rounds):
        running = threading.Barrier(workers_n + 1)
        stop = threading.Event()
        t = lumberjack.task("racing")
        handles.append(t)

        def worker(t=t, running=running, stop=stop) -> None:
            running.wait()
            while not stop.is_set():
                t.advance()

        workers = [threading.Thread(target=worker) for _ in range(workers_n)]
        for w in workers:
            w.start()
        running.wait()
        t.end()
        stop.set()
        for w in workers:
            w.join()

    rows = session.read()
    for t in handles:
        events = [r.task_event for r in rows if r.task_id == t.task_id]
        assert events[-1] == "end", f"an update landed after end: {events[-5:]}"
        assert events.count("end") == 1


def test_two_threads_cannot_both_enter_one_handle(session):
    """`__enter__` claims under the lock. Unsynchronized it is a
    check-then-act, and both threads passed — measured at 3 double-entries
    in 2000 attempts on a GIL build, one token silently clobbering the
    other's."""
    entered, rejected = [], []
    prev_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)  # widen the check-then-act window
    attempts = 2000
    try:
        for _ in range(attempts):
            handle = lumberjack.task("raced")
            ready = threading.Barrier(2)

            def enter(h=handle, b=ready) -> None:
                b.wait()
                try:
                    h.__enter__()
                    entered.append(1)
                except RuntimeError:
                    rejected.append(1)

            pair = [threading.Thread(target=enter) for _ in range(2)]
            for w in pair:
                w.start()
            for w in pair:
                w.join()
            handle.end()
            tracking._current_task.set(None)
    finally:
        sys.setswitchinterval(prev_interval)

    assert len(entered) == attempts, f"{len(entered)} entered, expected one each"
    assert len(rejected) == attempts


# --- level filtering --------------------------------------------------------


def test_a_level_the_logger_filters_out_emits_nothing(session):
    """`isEnabledFor` gates the record, as it would for any other log call."""
    logging.getLogger(TASK_LOGGER_NAME).setLevel(logging.CRITICAL)
    try:
        with lumberjack.task("quiet"):
            pass
        assert session.events() == []
    finally:
        logging.getLogger(TASK_LOGGER_NAME).setLevel(logging.NOTSET)


def test_a_task_filtered_out_at_start_emits_no_rows_at_all(make_session):
    """Filtering is per record and `end()` promotes to ERROR on failure, so
    without an all-or-nothing gate a failing task under `init(level=WARNING)`
    would write a lone `end` row with no `start` to anchor it."""
    with make_session(level=logging.WARNING) as live:
        with pytest.raises(ValueError):
            with lumberjack.task("filtered"):
                raise ValueError("boom")
        assert live.events() == []


def test_task_honours_an_explicit_level(session):
    with lumberjack.task("chatty", level=logging.WARNING):
        pass
    assert {r.level_name for r in session.read()} == {"WARNING"}


def test_a_subtask_inherits_its_parents_level(session):
    with lumberjack.task("parent", level=logging.WARNING) as parent:
        parent.subtask("child").end()
    child = [r for r in session.read() if r.task_label == "child"]
    assert child and {r.level_name for r in child} == {"WARNING"}


def test_a_level_raised_mid_task_still_leaves_the_start_row_anchored(session):
    """The other side of the all-or-nothing gate: once a `start` row exists,
    silencing the logger drops the later rows but cannot un-write it. A
    reader sees a task that started and never finished — which is true —
    rather than a contradiction."""
    logger = logging.getLogger(TASK_LOGGER_NAME)
    with lumberjack.task("interrupted") as t:
        logger.setLevel(logging.CRITICAL)
        try:
            t.advance()
        finally:
            pass
    logger.setLevel(logging.NOTSET)
    assert session.events() == [("start", "interrupted")]
