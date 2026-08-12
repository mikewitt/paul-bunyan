"""In-process unit tests for install/uninstall/idempotency/hook-chaining.

The excepthook-fires-for-real and atexit-fires-for-real paths can't be
exercised in-process (pytest owns exception handling; atexit only runs at
real interpreter shutdown) — those are covered via subprocess in
test_integration.py instead.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import pytest

from lumberjack import teardown
from lumberjack.handler import LumberjackHandler
from lumberjack.pump import FlushPump
from lumberjack.store import SQLiteRecordStore


class _FakeRenderer:
    """Lossy by default — a live display that swallows records is the case
    the diagnostic dump exists for."""

    def __init__(self, *, write_through: bool = False) -> None:
        self.write_through = write_through
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeHandler:
    def __init__(self, rows: list[str] | None = None) -> None:
        self._rows = list(rows or [])
        self.drained = 0
        self.peeked: list[int | None] = []

    def drain(self) -> list[str]:
        self.drained += 1
        rows, self._rows = self._rows, []
        return rows

    def peek(self, n: int | None = None) -> list[str]:
        self.peeked.append(n)
        return list(self._rows)


class _FakeStore:
    """Lossless and complete — the guarantee the exit dump is built on."""

    def __init__(self, rows: list[str] | None = None) -> None:
        self.rows: list[str] = list(rows or [])
        self.tailed: list[int] = []

    def append(self, rows: list[str]) -> None:
        self.rows.extend(rows)

    def tail(self, n: int) -> list[str]:
        self.tailed.append(n)
        return self.rows[-n:]


@pytest.fixture(autouse=True)
def _uninstall_after() -> None:
    yield
    teardown.uninstall()


def test_install_sets_excepthook():
    prev_hook = sys.excepthook
    teardown.install(
        renderer=_FakeRenderer(), handler=_FakeHandler(), store=_FakeStore()
    )
    assert sys.excepthook is teardown.handle_exception
    assert teardown.is_installed()
    teardown.uninstall()
    assert sys.excepthook is prev_hook
    assert not teardown.is_installed()


def test_install_twice_is_a_noop():
    renderer1, renderer2 = _FakeRenderer(), _FakeRenderer()
    handler, store = _FakeHandler(), _FakeStore()
    teardown.install(renderer=renderer1, handler=handler, store=store)
    teardown.install(renderer=renderer2, handler=handler, store=store)
    assert teardown.current_renderer() is renderer1


def test_excepthook_closes_renderer_before_delegating(monkeypatch):
    renderer = _FakeRenderer()
    handler = _FakeHandler()
    store = _FakeStore()
    calls: list[str] = []
    teardown.install(renderer=renderer, handler=handler, store=store)
    monkeypatch.setattr(
        teardown, "_prev_excepthook", lambda *a: calls.append("prev_hook")
    )
    teardown.handle_exception(RuntimeError, RuntimeError("x"), None)
    assert renderer.closed == 1
    assert calls == ["prev_hook"]


def test_teardown_flushes_buffer_to_store():
    renderer = _FakeRenderer()
    handler = _FakeHandler(rows=["a", "b"])
    store = _FakeStore()
    teardown.install(renderer=renderer, handler=handler, store=store)
    teardown.run()
    assert store.rows == ["a", "b"]
    assert handler.drained == 1


def test_teardown_is_idempotent():
    renderer = _FakeRenderer()
    handler = _FakeHandler(rows=["a"])
    store = _FakeStore()
    teardown.install(renderer=renderer, handler=handler, store=store)
    teardown.run()
    teardown.run()  # must not raise
    assert renderer.closed == 2


def test_lossy_renderer_dump_replays_the_store_tail_to_stderr(capsys, make_row):
    handler = _FakeHandler()
    store = _FakeStore(rows=[make_row(message="swallowed by the bar")])
    teardown.install(
        renderer=_FakeRenderer(write_through=False),
        handler=handler,
        store=store,
        dump_last_n=5,
    )
    teardown.run()
    assert "swallowed by the bar" in capsys.readouterr().err
    assert store.tailed == [5]
    assert handler.peeked == [], "the buffer is not a source for the dump"


def test_teardown_drains_before_dumping(capsys, make_row):
    # The dump reads the store, so anything still sitting in the buffer at exit
    # has to land there first — dump-then-drain would miss the run's whole tail.
    handler = _FakeHandler(rows=[make_row(message="still in the buffer")])
    teardown.install(
        renderer=_FakeRenderer(write_through=False),
        handler=handler,
        store=_FakeStore(),
        dump_last_n=5,
    )
    teardown.run()
    assert "still in the buffer" in capsys.readouterr().err


def test_dump_survives_the_flush_pump_draining_the_buffer(
    capsys, wait_until: Callable[..., bool]
):
    # The regression this ordering exists for: with the pump running, the
    # buffer is empty most of the time, so a buffer-sourced dump recovered
    # nothing. Real handler, real store, real pump — what init() builds.
    handler = LumberjackHandler(level=logging.DEBUG)
    store = SQLiteRecordStore(":memory:")
    pump = FlushPump(interval=0.001, flush=lambda: store.append(handler.drain()))
    logger = logging.getLogger("teardown-pump-test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    pump.start()
    try:
        for i in range(5):
            logger.info("swallowed by the bar %d", i)
        assert wait_until(lambda: len(store.recent()) == 5), "pump never drained"
        assert handler.peek() == [], "the pump emptied the buffer, as it does"
        teardown.install(
            renderer=_FakeRenderer(write_through=False),
            handler=handler,
            store=store,
            dump_last_n=50,
        )
        teardown.run()
    finally:
        pump.stop()
        logger.removeHandler(handler)
        store.close()
    err = capsys.readouterr().err
    assert "swallowed by the bar 0" in err
    assert "swallowed by the bar 4" in err


def test_write_through_renderer_is_not_dumped(make_row):
    # Regression: the atexit dump used to replay records the write-through
    # renderer had already printed, doubling every line of a normal run.
    store = _FakeStore(rows=[make_row(message="already printed")])
    teardown.install(
        renderer=_FakeRenderer(write_through=True),
        handler=_FakeHandler(),
        store=store,
        dump_last_n=5,
    )
    teardown.run()
    assert store.tailed == []


def test_dump_last_n_zero_disables_the_dump(make_row):
    store = _FakeStore(rows=[make_row()])
    teardown.install(
        renderer=_FakeRenderer(write_through=False),
        handler=_FakeHandler(),
        store=store,
        dump_last_n=0,
    )
    teardown.run()
    assert store.tailed == []


def test_uninstall_restores_previous_hook():
    prev_hook = sys.excepthook
    teardown.install(
        renderer=_FakeRenderer(), handler=_FakeHandler(), store=_FakeStore()
    )
    teardown.uninstall()
    assert sys.excepthook is prev_hook


def test_uninstall_without_install_is_a_noop():
    teardown.uninstall()
