from __future__ import annotations

import logging
import sqlite3
import sys
import threading

import pytest

import lumberjack
from lumberjack import teardown
from lumberjack.detect import OutputMode
from lumberjack.handler import LumberjackHandler
from lumberjack.store import SQLiteRecordStore


def _raise(exc: Exception):
    """A stand-in callable that fails, for exercising init()'s unwind path."""

    def _fail(*args: object, **kwargs: object) -> None:
        raise exc

    return _fail


def test_init_replaces_root_handlers_by_default():
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    lumberjack.init(output_mode="plain")
    assert sentinel not in root.handlers


def test_init_can_layer_instead_of_replace():
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        lumberjack.init(output_mode="plain", replace_handlers=False)
        assert sentinel in root.handlers
    finally:
        root.removeHandler(sentinel)


def test_double_init_raises():
    lumberjack.init(output_mode="plain")
    with pytest.raises(RuntimeError):
        lumberjack.init(output_mode="plain")


def test_shutdown_restores_previous_handlers():
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        lumberjack.init(output_mode="plain")
        lumberjack.shutdown()
        assert sentinel in root.handlers
    finally:
        root.removeHandler(sentinel)


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
    monkeypatch.setattr("lumberjack.renderers.rich_available", lambda: False)
    lumberjack.init(output_mode="rich")
    logger = logging.getLogger("no-rich-test")
    logger.info("still works")


def test_init_captures_log_records_into_buffer():
    handler = lumberjack.init(output_mode="plain")
    logger = logging.getLogger("capture-test")
    logger.info("captured")
    assert any(r.message == "captured" for r in handler.drain())


def test_shutdown_without_init_is_a_noop():
    lumberjack.shutdown()


def test_flush_without_init_is_a_noop():
    lumberjack.flush()


def test_is_initialized_tracks_lifecycle():
    assert not lumberjack.is_initialized()
    lumberjack.init(output_mode="plain")
    assert lumberjack.is_initialized()
    lumberjack.shutdown()
    assert not lumberjack.is_initialized()


def test_accessors_return_none_before_init():
    assert lumberjack.current_handler() is None
    assert lumberjack.current_store() is None
    assert lumberjack.current_renderer() is None
    assert lumberjack.current_output_mode() is None


def test_accessors_expose_live_components():
    handler = lumberjack.init(output_mode="plain")
    assert lumberjack.current_handler() is handler
    assert lumberjack.current_store() is not None
    assert lumberjack.current_renderer() is not None
    assert lumberjack.current_output_mode() is OutputMode.PLAIN


def test_current_output_mode_reflects_resolved_override():
    lumberjack.init(output_mode="json")
    assert lumberjack.current_output_mode() is OutputMode.JSON


def test_accessors_cleared_after_shutdown():
    lumberjack.init(output_mode="plain")
    lumberjack.shutdown()
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


def test_shutdown_leaves_a_caller_supplied_store_open():
    store = SQLiteRecordStore(":memory:")
    try:
        lumberjack.init(output_mode="plain", store=store)
        logging.getLogger("owned").info("still queryable")
        lumberjack.shutdown()
        assert any(r.message == "still queryable" for r in store.recent())
    finally:
        store.close()


def test_the_default_level_captures_debug():
    """The zero-config first run has to show the thing lumberjack is for.

    `logger.debug(...)` inside a loop is the product; any default above DEBUG
    has stdlib discard those calls before the handler sees them, so `init()`
    with no arguments would produce an empty display — Principle 1 failing on
    the exact case Principle 1 exists for. Issue #26.
    """
    store = SQLiteRecordStore(":memory:")
    lumberjack.init(store=store, output_mode="plain", flush_interval=0)
    try:
        logging.getLogger("zero-config").debug("the loop line")
        lumberjack.flush()
        assert [r.message for r in store.recent()] == ["the loop line"]
    finally:
        lumberjack.shutdown()
        store.close()


def test_a_quieter_capture_is_still_one_argument():
    """DEBUG is a default, not a mandate."""
    store = SQLiteRecordStore(":memory:")
    lumberjack.init(
        store=store, output_mode="plain", flush_interval=0, level=logging.INFO
    )
    try:
        log = logging.getLogger("quieter")
        log.debug("dropped")
        log.info("kept")
        lumberjack.flush()
        assert [r.message for r in store.recent()] == ["kept"]
    finally:
        lumberjack.shutdown()
        store.close()


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


# --- thread safety (#11) ----------------------------------------------------


def test_flush_during_shutdown_does_not_hit_a_closed_store():
    """The reachable race, and the reason this needs a lock at all.

    A worker calling `flush()` reads the session, then writes to that
    session's store. `shutdown()` closing the store in between raises
    `sqlite3.ProgrammingError` — not inside lumberjack, but out of the
    worker's own `flush()` call. This package is aimed squarely at concurrent
    programs, so that is not an exotic interleaving.

    Forced rather than raced: the store's `append` blocks until released, so
    the worker is provably inside the critical section when `shutdown()`
    starts. A timer releases it, because the main thread is by then blocked on
    the lock and cannot release anything itself.
    """
    lumberjack.init(output_mode="plain", flush_interval=0)
    store = lumberjack.current_store()
    assert store is not None

    inside_append = threading.Event()
    release = threading.Event()
    real_append = store.append

    def blocking_append(rows):
        inside_append.set()
        release.wait(5)
        real_append(rows)

    store.append = blocking_append
    logging.getLogger().warning("something to flush")

    escaped: list[BaseException] = []

    def worker() -> None:
        try:
            lumberjack.flush()
        except BaseException as exc:  # noqa: BLE001 - recording it is the test
            escaped.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert inside_append.wait(5), "worker never reached the store write"

    threading.Timer(0.2, release.set).start()
    lumberjack.shutdown()
    thread.join(5)

    assert not escaped, f"flush() raised into the caller: {escaped}"
