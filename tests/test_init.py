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
