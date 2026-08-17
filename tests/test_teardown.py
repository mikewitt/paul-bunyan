"""In-process unit tests for install/uninstall/idempotency/hook-chaining.

The excepthook-fires-for-real and atexit-fires-for-real paths can't be
exercised in-process (pytest owns exception handling; atexit only runs at
real interpreter shutdown) — those are covered via subprocess in
test_exit_paths.py instead.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from typing import cast

import pytest

from lumberjack import teardown
from lumberjack.detect import OutputMode
from lumberjack.handler import LumberjackHandler
from lumberjack.pump import FlushPump
from lumberjack.renderers import Renderer
from lumberjack.session import Session
from lumberjack.store import RecordStore, SQLiteRecordStore


class _FakeRenderer:
    """Lossy by default — a live display that swallows records is the case
    the diagnostic dump exists for."""

    def __init__(
        self, *, write_through: bool = False, suppressed_bars: int = 0
    ) -> None:
        self.write_through = write_through
        self.suppressed_bars = suppressed_bars
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeHandler:
    def __init__(self, rows: list[str] | None = None, dropped: int = 0) -> None:
        self._rows = list(rows or [])
        self.dropped = dropped
        self.drained = 0

    def drain(self) -> list[str]:
        self.drained += 1
        rows, self._rows = self._rows, []
        return rows


class _FakeStore:
    """Lossless and complete — the guarantee the exit dump is built on."""

    def __init__(self, rows: list[str] | None = None) -> None:
        self.rows: list[str] = list(rows or [])
        self.read: list[int | None] = []

    def append(self, rows: list[str]) -> None:
        self.rows.extend(rows)

    def recent(self, n: int | None = None, since: float | None = None) -> list[str]:
        self.read.append(n)
        return self.rows if n is None else self.rows[-n:]


def _install(
    *,
    renderer: object | None = None,
    handler: object | None = None,
    store: object | None = None,
    dump_last_n: int = 50,
) -> Session:
    """Install teardown over a Session of fakes, filling in what it ignores.

    The casts are the point of the fakes: teardown only ever calls `close()`,
    `drain()`, `dropped`, `append()` and `recent()`, so a stand-in exercising
    exactly those pins the contract more honestly than a real component would.
    """
    session = Session(
        handler=cast(LumberjackHandler, handler or _FakeHandler()),
        store=cast(RecordStore, store or _FakeStore()),
        renderer=cast(Renderer, renderer or _FakeRenderer()),
        output_mode=OutputMode.PLAIN,
        owns_store=False,
        dump_last_n=dump_last_n,
        prev_handlers=[],
        prev_level=logging.WARNING,
    )
    teardown.install(session)
    return session


@pytest.fixture(autouse=True)
def _uninstall_after() -> Iterator[None]:
    yield
    teardown.uninstall()


def test_install_sets_excepthook():
    prev_hook = sys.excepthook
    _install()
    assert sys.excepthook is teardown.handle_exception
    teardown.uninstall()
    assert sys.excepthook is prev_hook


def test_install_twice_raises():
    # There is one excepthook and one process exit to own, so a second
    # installer is asking for something this module cannot give it.
    _install()
    with pytest.raises(RuntimeError, match="already installed"):
        _install()


def test_a_rejected_install_leaves_the_first_session_intact():
    # Raising must not be the same as half-installing: the session teardown
    # still holds is the one whose renderer it closes.
    renderer1, renderer2 = _FakeRenderer(), _FakeRenderer()
    _install(renderer=renderer1)
    with pytest.raises(RuntimeError):
        _install(renderer=renderer2)
    teardown.run()
    assert (renderer1.closed, renderer2.closed) == (1, 0)


def test_install_works_again_after_uninstall():
    renderer1, renderer2 = _FakeRenderer(), _FakeRenderer()
    _install(renderer=renderer1)
    teardown.uninstall()
    _install(renderer=renderer2)
    teardown.run()
    assert (renderer1.closed, renderer2.closed) == (0, 1)


def test_excepthook_closes_renderer_before_delegating(monkeypatch):
    renderer = _FakeRenderer()
    handler = _FakeHandler()
    store = _FakeStore()
    calls: list[str] = []
    _install(renderer=renderer, handler=handler, store=store)
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
    _install(renderer=renderer, handler=handler, store=store)
    teardown.run()
    assert store.rows == ["a", "b"]
    assert handler.drained == 1


def test_the_buffer_reaches_the_store_before_the_display_closes():
    # A live bar draws its closing frame from the store. Close it first and
    # the run's final count is short — or, with the pump disabled, the store
    # is empty and the bar is never drawn at all.
    seen_at_close: list[list[str]] = []
    store = _FakeStore()

    class _RecordingRenderer(_FakeRenderer):
        def close(self) -> None:
            seen_at_close.append(list(store.rows))
            super().close()

    _install(
        renderer=_RecordingRenderer(),
        handler=_FakeHandler(rows=["a", "b", "c"]),
        store=store,
    )
    teardown.run()
    assert seen_at_close == [["a", "b", "c"]], "display closed over a stale store"


def test_teardown_is_idempotent():
    renderer = _FakeRenderer()
    handler = _FakeHandler(rows=["a"])
    store = _FakeStore()
    _install(renderer=renderer, handler=handler, store=store)
    teardown.run()
    teardown.run()  # must not raise
    assert renderer.closed == 2


def test_lossy_renderer_dump_replays_the_store_tail_to_stderr(capsys, make_row):
    handler = _FakeHandler()
    store = _FakeStore(rows=[make_row(message="swallowed by the bar")])
    _install(
        renderer=_FakeRenderer(write_through=False),
        handler=handler,
        store=store,
        dump_last_n=5,
    )
    teardown.run()
    assert "swallowed by the bar" in capsys.readouterr().err
    assert store.read == [5], "the dump's only source is the store"


def test_teardown_drains_before_dumping(capsys, make_row):
    # The dump reads the store, so anything still sitting in the buffer at exit
    # has to land there first — dump-then-drain would miss the run's whole tail.
    handler = _FakeHandler(rows=[make_row(message="still in the buffer")])
    _install(
        renderer=_FakeRenderer(write_through=False),
        handler=handler,
        store=_FakeStore(),
        dump_last_n=5,
    )
    teardown.run()
    assert "still in the buffer" in capsys.readouterr().err


def test_dump_survives_the_flush_pump_draining_the_buffer(
    capsys, wait_until: Callable[..., bool], attached_logger
):
    # The regression this ordering exists for: with the pump running, the
    # buffer is empty most of the time, so a buffer-sourced dump recovered
    # nothing. Real handler, real store, real pump — what init() builds.
    handler = LumberjackHandler(level=logging.DEBUG)
    store = SQLiteRecordStore(":memory:")
    pump = FlushPump(interval=0.001, flush=lambda: store.append(handler.drain()))
    pump.start()
    try:
        with attached_logger(handler, name="teardown-pump-test") as logger:
            for i in range(5):
                logger.info("swallowed by the bar %d", i)
            assert wait_until(lambda: len(store.recent()) == 5), "pump never drained"
            assert handler.drain() == [], "the pump emptied the buffer, as it does"
            _install(
                renderer=_FakeRenderer(write_through=False),
                handler=handler,
                store=store,
                dump_last_n=50,
            )
            teardown.run()
    finally:
        pump.stop()
        store.close()
    err = capsys.readouterr().err
    assert "swallowed by the bar 0" in err
    assert "swallowed by the bar 4" in err


def test_write_through_renderer_is_not_dumped(make_row):
    # Regression: the atexit dump used to replay records the write-through
    # renderer had already printed, doubling every line of a normal run.
    store = _FakeStore(rows=[make_row(message="already printed")])
    _install(
        renderer=_FakeRenderer(write_through=True),
        handler=_FakeHandler(),
        store=store,
        dump_last_n=5,
    )
    teardown.run()
    assert store.read == []


def test_dump_last_n_zero_disables_the_dump(make_row):
    store = _FakeStore(rows=[make_row()])
    _install(
        renderer=_FakeRenderer(write_through=False),
        handler=_FakeHandler(),
        store=store,
        dump_last_n=0,
    )
    teardown.run()
    assert store.read == []


def test_buffer_overflow_is_reported_at_exit(capsys):
    """Records dropped before the store are the one loss nothing else shows."""
    _install(
        renderer=_FakeRenderer(write_through=False),
        handler=_FakeHandler(dropped=37),
        store=_FakeStore(),
        dump_last_n=0,
    )
    teardown.run()
    err = capsys.readouterr().err
    assert "37 record(s) dropped" in err
    assert "buffer_size" in err


def test_overflow_report_is_not_suppressed_by_write_through(capsys):
    """The dump is skipped for write-through renderers; this warning isn't —
    it is about what never reached the store, not about what was displayed."""
    _install(
        renderer=_FakeRenderer(write_through=True),
        handler=_FakeHandler(dropped=2),
        store=_FakeStore(),
        dump_last_n=5,
    )
    teardown.run()
    assert "2 record(s) dropped" in capsys.readouterr().err


@pytest.mark.parametrize(
    "suppressed_bars, expected_substrings",
    [
        pytest.param(
            798,
            ["798 progress bar(s) hidden", "LUMBERJACK_MAX_BARS"],
            id="counts them and names the ceiling",
        ),
        pytest.param(
            5,
            ["hides rather than"],
            id="does not advise raising the ceiling",
        ),
    ],
)
def test_a_hidden_bar_count_is_reported_at_exit(
    capsys, suppressed_bars, expected_substrings
):
    """The ceiling is opt-in and quiet during the run, so exit is the only
    place a user learns part of the display was withheld — and the honest
    remedy is that the ceiling hides a grouping problem rather than fixing
    it, never "set a bigger number"."""
    _install(renderer=_FakeRenderer(suppressed_bars=suppressed_bars), dump_last_n=0)
    teardown.run()
    err = capsys.readouterr().err
    for substring in expected_substrings:
        assert substring in err


class _NoBars:
    """Plain renderers have no bars; the duck-typed read must not blow up."""

    write_through = True

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "renderer",
    [
        pytest.param(_FakeRenderer(suppressed_bars=0), id="nothing was hidden"),
        pytest.param(_NoBars(), id="the renderer has no ceiling to ask about"),
    ],
)
def test_no_hidden_bar_report_when_there_is_nothing_to_report(capsys, renderer):
    _install(renderer=renderer, dump_last_n=0)
    teardown.run()
    assert "progress bar(s) hidden" not in capsys.readouterr().err


def test_no_overflow_report_when_nothing_was_dropped(capsys):
    _install(
        renderer=_FakeRenderer(write_through=False),
        handler=_FakeHandler(dropped=0),
        store=_FakeStore(),
        dump_last_n=0,
    )
    teardown.run()
    assert "dropped" not in capsys.readouterr().err


def test_uninstall_restores_previous_hook():
    prev_hook = sys.excepthook
    _install()
    teardown.uninstall()
    assert sys.excepthook is prev_hook


def test_uninstall_without_install_is_a_noop():
    teardown.uninstall()
