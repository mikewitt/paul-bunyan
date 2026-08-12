"""The Phase 1 proof: a log line repeating inside a loop becomes a live bar.

These tests drive the real path — stdlib `logging` → `LumberjackHandler` →
`RecordStore` → bar — rather than poking the renderer directly, because the
premise being validated is end-to-end.

Timing is driven explicitly (`rig.tick()` stands in for the flush pump plus
the redraw timer) so nothing here sleeps; the one genuinely time-dependent
property, that a timer redraws without anyone asking, uses `wait_until`.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import threading
from collections.abc import Callable, Iterator

import pytest

pytest.importorskip("rich")

import lumberjack  # noqa: E402
import lumberjack.renderers.rich_renderer as rich_renderer_module  # noqa: E402
from lumberjack import teardown  # noqa: E402
from lumberjack.handler import LumberjackHandler  # noqa: E402
from lumberjack.renderers.rich_renderer import RichProgressRenderer  # noqa: E402
from lumberjack.store import RecordStore  # noqa: E402

_TIMER_THREAD = "lumberjack-progress"


def _timer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _TIMER_THREAD]


@dataclasses.dataclass
class _Rig:
    """A whole lumberjack pipeline, with the timers replaced by `tick()`."""

    logger: logging.Logger
    handler: LumberjackHandler
    store: RecordStore
    renderer: RichProgressRenderer
    stream: io.StringIO

    def tick(self) -> None:
        """One flush-pump drain plus one redraw, run synchronously."""
        self.store.append(self.handler.drain())
        self.renderer.refresh()

    def output(self) -> str:
        return self.stream.getvalue()


@pytest.fixture
def rig(store: RecordStore) -> Iterator[_Rig]:
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    handler = LumberjackHandler(on_record=renderer.render, level=logging.DEBUG)
    logger = logging.getLogger("progress-rig")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield _Rig(logger, handler, store, renderer, stream)
    finally:
        logger.removeHandler(handler)
        renderer.close()


@pytest.fixture
def as_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the renderer's console believe it is talking to a real terminal."""
    real_console = rich_renderer_module.Console
    monkeypatch.setattr(
        rich_renderer_module,
        "Console",
        lambda **kwargs: real_console(force_terminal=True, width=100, **kwargs),
    )


# --- the premise -----------------------------------------------------------


def test_a_loop_of_log_lines_becomes_one_advancing_bar(rig: _Rig):
    for i in range(50):
        rig.logger.info("processing item %d", i)
    rig.tick()

    (bar,) = rig.renderer.bars()
    assert bar.count == 50
    assert bar.label.startswith("test_render_progress.py:")
    assert bar.label.endswith("test_a_loop_of_log_lines_becomes_one_advancing_bar()")


def test_the_bar_advances_while_the_loop_runs(rig: _Rig):
    counts = []
    for i in range(30):
        rig.logger.debug("working %d", i)
        if i % 10 == 9:
            rig.tick()
            counts.append(rig.renderer.bars()[0].count)
    assert counts == [10, 20, 30]


def test_the_loop_lines_never_scroll(rig: _Rig):
    for i in range(50):
        rig.logger.info("processing item %d", i)
    rig.tick()
    # The whole point: a thousand lines in, one bar out.
    assert "processing item" not in rig.output()


def test_two_loops_get_two_bars(rig: _Rig):
    for i in range(5):
        rig.logger.info("reading %d", i)
    for i in range(4):
        rig.logger.info("writing %d", i)
    rig.tick()
    assert [b.count for b in rig.renderer.bars()] == [5, 4]


def test_a_one_off_line_gets_no_bar(rig: _Rig):
    rig.logger.info("started up")
    rig.tick()
    assert rig.renderer.bars() == []


def test_counts_come_from_the_store_not_the_callback(rig: _Rig):
    # Store, then render: records that never reached the store must not show
    # up in a bar, even though `render()` saw every one of them.
    for i in range(10):
        rig.logger.info("uncommitted %d", i)
    rig.renderer.refresh()  # redraw without draining the buffer first
    assert rig.renderer.bars() == []

    rig.tick()
    assert [b.count for b in rig.renderer.bars()] == [10]


def test_records_written_by_another_writer_reach_the_bar(rig: _Rig, make_row):
    # Same corollary from the other side: anything in the store counts, even
    # if this renderer's `render()` never saw it (a second thread, a second
    # process, or a future OTel bridge).
    rig.store.append([make_row() for _ in range(4)])
    rig.tick()
    assert [b.count for b in rig.renderer.bars()] == [4]


# --- lossy display, lossless store ----------------------------------------


def test_declares_itself_lossy():
    # Load-bearing: teardown's diagnostic dump keys off this flag, and this is
    # the first renderer that swallows records.
    assert RichProgressRenderer.write_through is False


def test_swallowed_records_are_still_in_the_store(rig: _Rig):
    for i in range(20):
        rig.logger.info("processing item %d", i)
    rig.tick()
    messages = [r.message for r in rig.store.recent()]
    assert len(messages) == 20
    assert messages[0] == "processing item 0"


def test_teardown_replays_swallowed_records_at_exit(rig: _Rig, capsys):
    for i in range(5):
        rig.logger.info("processing item %d", i)
    teardown.install(
        renderer=rig.renderer, handler=rig.handler, store=rig.store, dump_last_n=50
    )
    try:
        teardown.run()
    finally:
        teardown.uninstall()
    assert "processing item 4" in capsys.readouterr().err


def test_a_warning_still_prints_above_the_bars(rig: _Rig):
    for i in range(5):
        rig.logger.info("processing item %d", i)
    rig.logger.warning("disk is filling up")
    rig.tick()
    output = rig.output()
    assert "disk is filling up" in output
    assert "processing item" not in output


def test_a_traceback_still_prints_above_the_bars(rig: _Rig):
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        rig.logger.exception("request failed")
    assert "RuntimeError: boom" in rig.output()


def test_records_below_the_passthrough_level_are_collapsed(
    store: RecordStore, make_row
):
    renderer = RichProgressRenderer(
        store,
        stream=(stream := io.StringIO()),
        refresh_interval=0,
        passthrough_level=logging.ERROR,
    )
    try:
        renderer.render(make_row(level_name="WARNING", level_no=logging.WARNING))
        assert stream.getvalue() == ""
        renderer.render(make_row(level_name="ERROR", level_no=logging.ERROR))
        assert "hello world" in stream.getvalue()
    finally:
        renderer.close()


# --- redraw cadence --------------------------------------------------------


def test_log_volume_never_triggers_a_redraw(rig: _Rig, monkeypatch):
    redraws = 0

    def count_redraw() -> None:
        nonlocal redraws
        redraws += 1

    def burst() -> None:
        for i in range(500):
            rig.logger.info("processing item %d", i)

    monkeypatch.setattr(rig.renderer._progress, "refresh", count_redraw)
    burst()
    assert redraws == 0, "a record must never reach the display on its own"

    rig.tick()  # the first tick also adds the bar, which rich redraws for itself
    redraws = 0
    burst()
    assert redraws == 0

    rig.tick()
    assert redraws == 1  # 1000 records, one redraw per timer tick


def test_the_timer_redraws_without_being_asked(
    store: RecordStore, make_row, wait_until: Callable[..., bool]
):
    renderer = RichProgressRenderer(
        store, stream=io.StringIO(), min_repeats=3, refresh_interval=0.01
    )
    try:
        store.append([make_row() for _ in range(6)])
        assert wait_until(lambda: [b.count for b in renderer.bars()] == [6])
    finally:
        renderer.close()


def test_a_zero_interval_starts_no_timer(store: RecordStore):
    renderer = RichProgressRenderer(store, stream=io.StringIO(), refresh_interval=0)
    try:
        assert _timer_threads() == []
    finally:
        renderer.close()


# --- teardown --------------------------------------------------------------


def test_close_leaves_no_timer_thread_behind(store: RecordStore):
    renderer = RichProgressRenderer(store, stream=io.StringIO(), refresh_interval=0.01)
    renderer.close()
    assert _timer_threads() == []


def test_close_is_idempotent(store: RecordStore):
    renderer = RichProgressRenderer(store, stream=io.StringIO(), refresh_interval=0)
    renderer.close()
    renderer.close()


def test_close_draws_a_final_frame(rig: _Rig):
    # Whatever the timer last drew is stale by the time a run ends; the counts
    # it finished on are the ones worth leaving on screen.
    for i in range(9):
        rig.logger.info("processing item %d", i)
    rig.store.append(rig.handler.drain())
    rig.renderer.close()
    assert [b.count for b in rig.renderer.bars()] == [9]


def test_close_survives_a_store_that_closed_first(store: RecordStore, make_row):
    store.append([make_row() for _ in range(5)])
    renderer = RichProgressRenderer(store, stream=io.StringIO(), refresh_interval=0)
    store.close()
    renderer.close()  # must not raise: the display still has to be handed back


def test_render_and_refresh_after_close_do_nothing(store: RecordStore, make_row):
    stream = io.StringIO()
    renderer = RichProgressRenderer(store, stream=stream, refresh_interval=0)
    store.append([make_row() for _ in range(5)])
    renderer.close()
    before = stream.getvalue()
    renderer.refresh()
    renderer.render(make_row(level_name="ERROR", level_no=logging.ERROR))
    assert stream.getvalue() == before


def test_live_display_hides_then_restores_the_cursor(
    as_terminal, store: RecordStore, make_row
):
    # Never corrupt a traceback: the display must give the terminal back
    # before anything else prints.
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    store.append([make_row() for _ in range(5)])
    renderer.refresh()
    assert "\x1b[?25l" in stream.getvalue()  # cursor hidden while live
    renderer.close()
    assert stream.getvalue().endswith("\x1b[?25h")  # and handed back


def test_the_bar_is_actually_drawn_on_a_terminal(
    as_terminal, store: RecordStore, make_row
):
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row() for _ in range(5)])
        renderer.refresh()
        assert "foo.py:10 bar()" in stream.getvalue()
        assert "5 records" in stream.getvalue()
    finally:
        renderer.close()


# --- degrades, never errors ------------------------------------------------


def test_raises_a_clear_error_without_rich(monkeypatch, store: RecordStore):
    # The factory never reaches this path without rich; it is the guard for
    # anyone constructing the renderer directly.
    monkeypatch.setattr(rich_renderer_module, "Progress", None)
    with pytest.raises(RuntimeError, match="rich is not installed"):
        RichProgressRenderer(store, stream=io.StringIO())


# --- through the public API ------------------------------------------------


def test_init_turns_a_logging_loop_into_a_bar(store: RecordStore):
    lumberjack.init(output_mode="rich", store=store, flush_interval=0)
    renderer = lumberjack.current_renderer()
    assert isinstance(renderer, RichProgressRenderer)

    logger = logging.getLogger("init-loop")
    for i in range(40):
        logger.info("processing item %d", i)

    lumberjack.flush()
    renderer.refresh()
    assert [b.count for b in renderer.bars()] == [40]


def test_shutdown_stops_the_live_display(store: RecordStore):
    lumberjack.init(output_mode="rich", store=store)
    assert _timer_threads()
    lumberjack.shutdown()
    assert _timer_threads() == []
