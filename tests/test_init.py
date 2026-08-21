"""`init()`/`shutdown()`/`flush()` lifecycle.

Covers: taking over and restoring the root logger's handlers and level,
unwinding cleanly when some step of `init()` fails partway through, the
`current_*()` accessors across the before/during/after lifecycle, store
ownership (a store `init()` created itself is closed on `shutdown()`; a
caller-supplied one is left open), and the DEBUG-by-default capture level
that makes the zero-config first run show anything at all. Thread-safety
around concurrent `flush()`/`shutdown()` calls (#11) lives in
`test_session.py` instead.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading

import pytest

import lumberjack
from lumberjack import teardown
from lumberjack.detect import OutputMode
from lumberjack.handler import DEFAULT_BUFFER_SIZE, LumberjackHandler
from lumberjack.store import (
    DEFAULT_RETAIN,
    MIN_RETAIN,
    RETENTION_SLACK,
    RecordStore,
    SQLiteRecordStore,
)


def _raise(exc: Exception):
    """A stand-in callable that fails, for exercising init()'s unwind path."""

    def _fail(*args: object, **kwargs: object) -> None:
        raise exc

    return _fail


def test_init_replaces_root_handlers_by_default(root_sentinel):
    lumberjack.init(output_mode="plain")
    assert root_sentinel not in logging.getLogger().handlers


def test_init_can_layer_instead_of_replace(root_sentinel):
    lumberjack.init(output_mode="plain", replace_handlers=False)
    assert root_sentinel in logging.getLogger().handlers


def test_double_init_raises():
    lumberjack.init(output_mode="plain")
    with pytest.raises(RuntimeError):
        lumberjack.init(output_mode="plain")


def test_shutdown_restores_previous_handlers(root_sentinel):
    lumberjack.init(output_mode="plain")
    lumberjack.shutdown()
    assert root_sentinel in logging.getLogger().handlers


def test_shutdown_restores_the_root_level():
    """init() takes over the level too, so shutdown() has to give it back."""
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    lumberjack.init(level=logging.DEBUG, output_mode="plain")
    assert root.level == logging.DEBUG
    lumberjack.shutdown()
    assert root.level == logging.WARNING


def test_failed_init_closes_the_store_it_created(monkeypatch):
    """A store nobody can reach is a leak; init() must not leave one behind."""
    created: list[SQLiteRecordStore] = []
    real_init = SQLiteRecordStore.__init__

    def spy(self, path=":memory:"):
        real_init(self, path)
        created.append(self)

    monkeypatch.setattr(SQLiteRecordStore, "__init__", spy)
    monkeypatch.setattr(
        lumberjack, "create_renderer", _raise(RuntimeError("renderer boom"))
    )

    with pytest.raises(RuntimeError, match="renderer boom"):
        lumberjack.init(output_mode="plain")

    assert len(created) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].recent()


def test_failed_init_leaves_the_root_logger_alone(monkeypatch):
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    root.setLevel(logging.WARNING)
    monkeypatch.setattr(
        lumberjack, "create_renderer", _raise(RuntimeError("renderer boom"))
    )
    try:
        with pytest.raises(RuntimeError, match="renderer boom"):
            lumberjack.init(level=logging.DEBUG, output_mode="plain")
        assert sentinel in root.handlers
        assert root.level == logging.WARNING
        assert not lumberjack.is_initialized()
    finally:
        root.removeHandler(sentinel)


def test_init_is_retryable_after_a_failure(monkeypatch):
    """A failed init() must not wedge the module into a half-installed state."""
    monkeypatch.setattr(
        lumberjack, "create_renderer", _raise(RuntimeError("renderer boom"))
    )
    with pytest.raises(RuntimeError, match="renderer boom"):
        lumberjack.init(output_mode="plain")
    monkeypatch.undo()
    lumberjack.init(output_mode="plain")
    assert lumberjack.is_initialized()


def test_a_bad_level_fails_before_teardown_is_installed():
    """Everything that can raise must do so before `teardown.install()`.

    Not hypothetical: `init(level=...)` is validated by `logging`, and
    `teardown.install()` now raises on a second call rather than shrugging.
    Install teardown before something that can still fail and a failed
    `init()` would leave it owning the excepthook with no session to reach
    it, turning the retry into a confusing "already installed".
    """
    with pytest.raises(ValueError, match="Unknown level"):
        lumberjack.init(level="LOUD", output_mode="plain")
    assert (
        sys.excepthook is not teardown.handle_exception
    ), "teardown outlived a failed init() and still owns the excepthook"
    assert not any(
        isinstance(h, LumberjackHandler) for h in logging.getLogger().handlers
    ), "a failed init() left its handler on the root logger"


def test_init_without_rich_installed_still_works(monkeypatch):
    """Principle 9's degradation, exercised end to end: asking for `rich`
    when it is not importable must not raise, and the record still reaches
    the (now plain) store."""
    monkeypatch.setattr("lumberjack.renderers.rich_available", lambda: False)
    store = SQLiteRecordStore(":memory:")
    lumberjack.init(store=store, output_mode="rich", flush_interval=0)
    try:
        logging.getLogger("no-rich-test").info("still works")
        lumberjack.flush()
        assert any(r.message == "still works" for r in store.recent())
    finally:
        lumberjack.shutdown()
        store.close()


def test_shutdown_without_init_is_a_noop():
    lumberjack.shutdown()


def test_flush_without_init_is_a_noop():
    lumberjack.flush()


def test_accessors_walk_the_full_lifecycle():
    """Before `init()`: every accessor is None. During: each is live and
    consistent with what `init()` returned. After `shutdown()`: cleared
    again — the three stages of `is_initialized()` and of every accessor,
    walked together so they can only ever be pinned in agreement."""
    assert not lumberjack.is_initialized()
    assert lumberjack.current_handler() is None
    assert lumberjack.current_store() is None
    assert lumberjack.current_renderer() is None
    assert lumberjack.current_output_mode() is None

    handler = lumberjack.init(output_mode="plain")
    assert lumberjack.is_initialized()
    assert lumberjack.current_handler() is handler
    assert lumberjack.current_store() is not None
    assert lumberjack.current_renderer() is not None
    assert lumberjack.current_output_mode() is OutputMode.PLAIN

    lumberjack.shutdown()
    assert not lumberjack.is_initialized()
    assert lumberjack.current_handler() is None
    assert lumberjack.current_store() is None
    assert lumberjack.current_renderer() is None
    assert lumberjack.current_output_mode() is None


def test_records_reach_the_store_without_an_explicit_flush(wait_until):
    # The store must be readable *during* a run: analysis and any store-reading
    # renderer have nothing to work with if it only fills at exit.
    lumberjack.init(output_mode="plain", flush_interval=0.01)
    logging.getLogger("pumped").info("pumped through")
    store = lumberjack.current_store()
    assert store is not None
    assert wait_until(
        lambda: any(r.message == "pumped through" for r in store.recent())
    )


def test_flush_interval_zero_disables_the_pump(wait_until):
    lumberjack.init(output_mode="plain", flush_interval=0)
    logging.getLogger("unpumped").info("buffered only")
    store = lumberjack.current_store()
    assert store is not None
    assert not wait_until(lambda: len(store.recent()) > 0, timeout=0.2)
    lumberjack.flush()
    assert any(r.message == "buffered only" for r in store.recent())


def test_shutdown_closes_a_store_lumberjack_created():
    lumberjack.init(output_mode="plain")
    store = lumberjack.current_store()
    assert store is not None
    lumberjack.shutdown()
    with pytest.raises(sqlite3.ProgrammingError):
        store.recent()


def test_shutdown_leaves_a_caller_supplied_store_open(make_session):
    with make_session():
        store = lumberjack.current_store()
        assert store is not None
        logging.getLogger("owned").info("still queryable")
        lumberjack.shutdown()
        assert any(r.message == "still queryable" for r in store.recent())


def test_the_default_level_captures_debug(make_session):
    """The zero-config first run has to show the thing lumberjack is for.

    `logger.debug(...)` inside a loop is the product; any default above DEBUG
    has stdlib discard those calls before the handler sees them, so `init()`
    with no arguments would produce an empty display — Principle 1 failing on
    the exact case Principle 1 exists for. Issue #26.
    """
    with make_session():
        logging.getLogger("zero-config").debug("the loop line")
        lumberjack.flush()
        store = lumberjack.current_store()
        assert store is not None
        assert [r.message for r in store.recent()] == ["the loop line"]


def test_a_quieter_capture_is_still_one_argument(make_session):
    """DEBUG is a default, not a mandate."""
    with make_session(level=logging.INFO):
        log = logging.getLogger("quieter")
        log.debug("dropped")
        log.info("kept")
        lumberjack.flush()
        store = lumberjack.current_store()
        assert store is not None
        assert [r.message for r in store.recent()] == ["kept"]


def test_shutdown_stops_the_live_display(store: RecordStore):
    """`init()`'s wiring from `shutdown()` through to the renderer: the live
    display's redraw timer must not outlive the session that started it."""
    pytest.importorskip("rich")
    lumberjack.init(output_mode="rich", store=store)
    assert [t for t in threading.enumerate() if t.name == "lumberjack-progress"]
    lumberjack.shutdown()
    assert not [t for t in threading.enumerate() if t.name == "lumberjack-progress"]


def test_a_store_init_created_is_closed_when_teardown_refuses(monkeypatch):
    """`teardown.install()` raises when it is already installed, and it is the
    last thing `init()` does that can fail. A store `init()` created itself
    has to be closed on that path too.

    Asserted on the close rather than on a ResourceWarning: unclosed sqlite
    connections only warn from 3.13, so a warning-based test would pass on
    3.12 whether or not the store was closed.
    """
    closed: list[bool] = []

    class _WatchedStore(SQLiteRecordStore):
        def close(self) -> None:
            closed.append(True)
            super().close()

    monkeypatch.setattr(lumberjack, "SQLiteRecordStore", _WatchedStore)
    monkeypatch.setattr(
        teardown, "install", _raise(RuntimeError("teardown already installed"))
    )
    with pytest.raises(RuntimeError, match="teardown already installed"):
        lumberjack.init(output_mode="plain")
    assert closed == [True], "init() leaked the store it created"


class _FailOnceStore(SQLiteRecordStore):
    """Refuses the first `append()` and takes every one after it.

    A subclass rather than a stub, so the retry lands in a real store and the
    assertion is that the records are *there* rather than that a mock was
    called.
    """

    def __init__(self) -> None:
        super().__init__(":memory:")
        self.failures = 0

    def append(self, rows):
        if self.failures == 0:
            self.failures += 1
            raise RuntimeError("store is unwritable")
        super().append(rows)


def test_a_failed_write_keeps_the_records_for_the_next_flush():
    """Principle 6's lossless half, at the buffer→store seam (#77).

    `drain()` empties the buffer before `append()` is attempted, so a raising
    store used to lose the batch with nothing holding it and nothing counting
    it. The exception still propagates: the pump swallows it and tries again,
    and a caller flushing by hand deserves to hear that the store is failing.
    """
    store = _FailOnceStore()
    lumberjack.init(store=store, output_mode="plain", flush_interval=0)
    try:
        logging.getLogger("retry-test").info("survives a failed write")
        with pytest.raises(RuntimeError, match="unwritable"):
            lumberjack.flush()
        assert store.recent() == [], "nothing was written by the failed attempt"

        lumberjack.flush()
        assert [r.message for r in store.recent()] == ["survives a failed write"]
    finally:
        lumberjack.shutdown()
        store.close()


# --- the store is bounded, and the bound is stated (#109) ------------------
#
# `evict()` shipped fully implemented, index-optimised and tested, and
# nothing ever called it — so the default `:memory:` store accumulated every
# record for the life of the process. Measured at 319 bytes a record: 304 MiB
# at a million, ~3 GiB at ten. The scenario is not exotic, it is the one the
# package exists for.


#: Records per flush. Below `DEFAULT_BUFFER_SIZE`, because the write buffer
#: is bounded and evicts under pressure: with the pump disabled, logging more
#: than it holds before flushing measures how fast lumberjack *discards* a
#: record rather than how many it keeps. That is a different question, and it
#: silently caps every count here at the buffer size.
_FLUSH_EVERY = DEFAULT_BUFFER_SIZE // 4


def _log_n(n: int, *, logger: str = "retention-test") -> None:
    """Log `n` records through the real path, draining as the pump would."""
    log = logging.getLogger(logger)
    for i in range(n):
        log.info("row %d processed", i)
        if (i + 1) % _FLUSH_EVERY == 0:
            lumberjack.flush()
    lumberjack.flush()
    handler = lumberjack.current_handler()
    assert handler is not None
    assert handler.dropped == 0, "the buffer evicted; this is not a retention test"


def test_the_store_is_bounded_by_retain():
    """The observable contract: how many records a caller can read back.

    Not "was `evict()` called" — that is the mechanism, and the mechanism is
    free to change.
    """
    lumberjack.init(output_mode="plain", flush_interval=0, retain=MIN_RETAIN)
    try:
        _log_n(MIN_RETAIN * 3)
        store = lumberjack.current_store()
        assert store is not None
        held = store.recent(n=None)
        assert len(held) <= MIN_RETAIN + MIN_RETAIN // RETENTION_SLACK
    finally:
        lumberjack.shutdown()


def test_what_survives_eviction_is_the_newest():
    """Eviction is by arrival order and never by content, so the records a
    reader most likely wants — the ones nearest whatever just went wrong —
    are the ones kept."""
    lumberjack.init(output_mode="plain", flush_interval=0, retain=MIN_RETAIN)
    try:
        total = MIN_RETAIN * 3
        _log_n(total)
        store = lumberjack.current_store()
        assert store is not None
        held = store.recent(n=None)
        assert held[-1].message == f"row {total - 1} processed"
        assert held[0].message != "row 0 processed", "the oldest were not evicted"
    finally:
        lumberjack.shutdown()


def test_retention_leaves_room_before_it_trims():
    """The slack is what makes this amortised rather than paid per drain.

    `evict(keep_last=N)` walks N index entries to resolve its cutoff, so
    trimming on every 200ms drain would cost a `retain`-sized walk five times
    a second. Just over the bound must therefore still be untrimmed.
    """
    lumberjack.init(output_mode="plain", flush_interval=0, retain=MIN_RETAIN)
    try:
        _log_n(MIN_RETAIN + 1)
        store = lumberjack.current_store()
        assert store is not None
        assert len(store.recent(n=None)) == MIN_RETAIN + 1
    finally:
        lumberjack.shutdown()


def test_retain_none_keeps_everything():
    """The opt-out, for an application that wants the old behaviour or has a
    retention policy of its own."""
    lumberjack.init(output_mode="plain", flush_interval=0, retain=None)
    try:
        _log_n(MIN_RETAIN * 2)
        store = lumberjack.current_store()
        assert store is not None
        assert len(store.recent(n=None)) == MIN_RETAIN * 2
    finally:
        lumberjack.shutdown()


def test_a_caller_supplied_store_is_trimmed_too():
    """Eviction is not the same act as closing, so it does not follow the
    same ownership rule.

    `shutdown()` leaves a caller's store open because closing makes it
    unusable afterwards; a trimmed store is entirely usable and holds the
    newest `retain` records. Exempting caller-supplied stores would exempt
    every long-running configuration that actually needs the bound, since
    passing a file-backed store is how durable capture is done.
    """
    store = SQLiteRecordStore(":memory:")
    lumberjack.init(
        store=store, output_mode="plain", flush_interval=0, retain=MIN_RETAIN
    )
    try:
        _log_n(MIN_RETAIN * 3)
        assert len(store.recent(n=None)) <= MIN_RETAIN + MIN_RETAIN // RETENTION_SLACK
    finally:
        lumberjack.shutdown()
        store.close()


@pytest.mark.parametrize(
    "retain",
    [0, -1, 1, 999, MIN_RETAIN - 1, 1.5, "lots"],
)
def test_a_retain_the_display_cannot_survive_is_rejected(retain: object):
    """A bad argument is a caller's bug and raises; only a bad environment
    variable warns and degrades.

    The floor is not arbitrary. The display's models resume from a rowid
    watermark that lags by up to one refresh interval, so a `retain` small
    enough for a burst to be evicted between two redraws makes a bar
    under-count.
    """
    with pytest.raises(ValueError, match="retain"):
        lumberjack.init(output_mode="plain", retain=retain)  # type: ignore[arg-type]
    assert not lumberjack.is_initialized(), "a rejected argument still installed"


def test_the_default_retain_is_the_number_the_docs_claim():
    """`store.py` and `CLAUDE.md` both said "the ~1M-record retention target"
    while nothing enforced it. Choosing that number is what made the prose
    true rather than adding a second figure to reconcile."""
    assert DEFAULT_RETAIN == 1_000_000


class _CountingStore(SQLiteRecordStore):
    """A real store that also records how often it was asked to evict.

    A subclass rather than a stub, so the rows really are trimmed and the
    count describes work that actually happened.
    """

    def __init__(self) -> None:
        super().__init__(":memory:")
        self.evictions = 0

    def evict(self, *, before=None, keep_last=None):
        self.evictions += 1
        return super().evict(before=before, keep_last=keep_last)


def test_retention_is_amortised_rather_than_paid_on_every_drain():
    """The slack has to keep working after the first trim, not just before it.

    Found by mutation: dropping `session.stored -= deleted` leaves the store
    correctly bounded and every other test green, because the *rows* are
    still trimmed — what regresses is that the count never falls back below
    the threshold, so every drain from then on asks the store to evict again.
    Measured, that is a 14ms cutoff scan five times a second to delete almost
    nothing.

    So the contract is about how often the store is asked, and counting is
    the honest way to assert it.

    The drain has to be smaller than the slack for there to be anything to
    amortise — a drain that adds more than the slack pushes past the
    threshold on its own, every time, and correctly trims. At the default
    bound that is unreachable: the slack is 100,000 records and the write
    buffer holds 10,000, so a drain cannot deliver enough. Here the bound is
    the floor, so the batch is sized down to match.
    """
    store = _CountingStore()
    slack = MIN_RETAIN // RETENTION_SLACK
    per_flush = slack // 5
    flushes = 80
    lumberjack.init(
        store=store, output_mode="plain", flush_interval=0, retain=MIN_RETAIN
    )
    try:
        log = logging.getLogger("retention-test")
        for batch in range(flushes):
            for i in range(per_flush):
                log.info("row %d processed", batch * per_flush + i)
            lumberjack.flush()
    finally:
        lumberjack.shutdown()
        evictions = store.evictions
        store.close()

    # A fifth of the slack per flush, so a trim is due about every fifth one
    # once the bound is reached. One per flush is the defect.
    assert (
        0 < evictions <= flushes // RETENTION_SLACK
    ), f"{evictions} evictions over {flushes} flushes"


def test_a_pre_populated_store_still_gets_trimmed_repeatedly(make_row):
    """The row count must not drift when the store holds rows nobody counted.

    `init()` accepts a store the caller built, and it may already hold
    records this session never wrote — reopening a file-backed one is the
    ordinary way to get durable capture. Subtracting `evict()`'s return value
    from the session's own count looks equivalent to assigning the bound and
    is not: the first trim deletes far more than the session ever wrote, the
    count goes negative, and retention then does not fire again until the
    session has written that whole difference a second time.

    So a second trim is the assertion, not the first one.
    """
    store = _CountingStore()
    # Rows from a "previous run", written straight to the store so the
    # session has no idea they exist.
    prior = MIN_RETAIN * 4
    log = logging.getLogger("retention-test")
    store.append([make_row(message="a row from a previous run") for _ in range(prior)])

    lumberjack.init(
        store=store, output_mode="plain", flush_interval=0, retain=MIN_RETAIN
    )
    try:
        # Five flushes of half the bound: the first trim is due on the third,
        # and every flush after it. Under the defect there is exactly one.
        for _ in range(5):
            for i in range(MIN_RETAIN // 2):
                log.info("row %d processed", i)
            lumberjack.flush()
        held = len(store.recent(n=None))
    finally:
        lumberjack.shutdown()
        evictions = store.evictions
        store.close()

    assert evictions >= 2, f"retention stopped after {evictions} trim(s)"
    assert held <= MIN_RETAIN + MIN_RETAIN // RETENTION_SLACK
