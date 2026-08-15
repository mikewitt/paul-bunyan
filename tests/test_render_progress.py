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
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator

import pytest

pytest.importorskip("rich")

import lumberjack  # noqa: E402
import lumberjack.renderers.rich_renderer as rich_renderer_module  # noqa: E402
from lumberjack import (
    session,  # noqa: E402
    teardown,  # noqa: E402
)
from lumberjack.detect import OutputMode  # noqa: E402
from lumberjack.handler import LumberjackHandler  # noqa: E402
from lumberjack.renderers.rich_renderer import RichProgressRenderer  # noqa: E402
from lumberjack.session import Session  # noqa: E402
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


# --- the opt-in bar ceiling ------------------------------------------------


def _capped_renderer(store: RecordStore, max_bars: int) -> RichProgressRenderer:
    return RichProgressRenderer(
        store,
        stream=io.StringIO(),
        min_repeats=3,
        refresh_interval=0,
        max_bars=max_bars,
    )


def test_the_ceiling_bounds_what_is_drawn(store: RecordStore, make_row):
    renderer = _capped_renderer(store, max_bars=2)
    try:
        for lineno in range(5):
            store.append([make_row(lineno=lineno) for _ in range(3)])
        renderer.refresh()
        assert len(renderer._tasks) == 2, "drew more bars than the ceiling allows"
    finally:
        renderer.close()


def test_the_ceiling_does_not_touch_the_counts(store: RecordStore, make_row):
    # The model tracks every source regardless; the ceiling is a property of
    # the display. A capped run must not misreport what it captured.
    renderer = _capped_renderer(store, max_bars=2)
    try:
        for lineno in range(5):
            store.append([make_row(lineno=lineno) for _ in range(3)])
        renderer.refresh()
        assert len(renderer.bars()) == 5
        assert sum(b.count for b in renderer.bars()) == 15
    finally:
        renderer.close()


def test_the_ceiling_counts_what_it_hid(store: RecordStore, make_row):
    renderer = _capped_renderer(store, max_bars=2)
    try:
        assert renderer.suppressed_bars == 0
        for lineno in range(5):
            store.append([make_row(lineno=lineno) for _ in range(3)])
        renderer.refresh()
        assert renderer.suppressed_bars == 3
    finally:
        renderer.close()


def test_no_ceiling_draws_everything(store: RecordStore, make_row, monkeypatch):
    monkeypatch.delenv("LUMBERJACK_MAX_BARS", raising=False)
    renderer = RichProgressRenderer(
        store, stream=io.StringIO(), min_repeats=3, refresh_interval=0
    )
    try:
        for lineno in range(5):
            store.append([make_row(lineno=lineno) for _ in range(3)])
        renderer.refresh()
        assert len(renderer._tasks) == 5
        assert renderer.suppressed_bars == 0
    finally:
        renderer.close()


def test_the_environment_sets_the_ceiling(store: RecordStore, make_row, monkeypatch):
    # The whole point of the knob: reachable without touching init().
    monkeypatch.setenv("LUMBERJACK_MAX_BARS", "1")
    renderer = RichProgressRenderer(
        store, stream=io.StringIO(), min_repeats=3, refresh_interval=0
    )
    try:
        for lineno in range(4):
            store.append([make_row(lineno=lineno) for _ in range(3)])
        renderer.refresh()
        assert len(renderer._tasks) == 1
        assert renderer.suppressed_bars == 3
    finally:
        renderer.close()


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
        Session(
            handler=rig.handler,
            store=rig.store,
            renderer=rig.renderer,
            output_mode=OutputMode.RICH,
            owns_store=False,
            dump_last_n=50,
            prev_handlers=[],
            prev_level=logging.WARNING,
        )
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

    monkeypatch.setattr(rig.renderer._live, "refresh", count_redraw)
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


# --- named bars from the tracking API --------------------------------------
#
# The tracking API is inert without a session, so these need a published one —
# and its records reach the handler by way of the *root* logger, which is
# where `init()` would have put it.


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)


@pytest.fixture
def task_rig(as_terminal: None, store: RecordStore) -> Iterator[_Rig]:
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    handler = LumberjackHandler(on_record=renderer.render, level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logger = logging.getLogger("progress-task-rig")
    logger.setLevel(logging.DEBUG)
    session.set_current_session(
        Session(
            handler=handler,
            store=store,
            renderer=renderer,
            output_mode=OutputMode.RICH,
            owns_store=False,
            dump_last_n=0,
            prev_handlers=[],
            prev_level=root.level,
        )
    )
    try:
        yield _Rig(logger, handler, store, renderer, stream)
    finally:
        session.set_current_session(None)
        root.removeHandler(handler)
        renderer.close()


def test_a_task_draws_a_named_determinate_bar(task_rig):
    """Rung 2 on screen: the label the code chose, and a real percentage,
    because `task()` said how much work there was."""
    with lumberjack.task("reindex", total=100) as t:
        t.set_progress(40)
    task_rig.tick()
    out = _strip_ansi(task_rig.output())
    assert "reindex" in out
    assert "40/100" in out
    assert "40%" in out


def test_a_task_without_a_total_pulses_instead_of_claiming_one(task_rig):
    """`total=None` is rich's indeterminate bar. Inventing a denominator
    would be the one thing a bar must never do."""
    with lumberjack.task("scan") as t:
        t.advance()
    task_rig.tick()
    out = _strip_ansi(task_rig.output())
    assert "scan" in out
    assert "%" not in out, "an indeterminate task must not show a percentage"


def test_a_container_task_shows_no_count_at_all(task_rig):
    """A bare 0 beside a pulsing bar reads as "stuck at zero" rather than
    "no count was claimed"."""
    handle = lumberjack.task("etl run")
    try:
        task_rig.tick()
        line = next(
            ln for ln in _strip_ansi(task_rig.output()).splitlines() if "etl run" in ln
        )
        # Everything but the elapsed clock, which is always digits.
        assert not re.search(r"\d", re.sub(r"\d+:\d\d:\d\d", "", line))
    finally:
        handle.end()


def test_subtasks_are_indented_under_their_parent(task_rig):
    with lumberjack.task("outer") as outer:
        with outer.subtask("inner"):
            task_rig.tick()
    out = _strip_ansi(task_rig.output())
    outer_line = next(ln for ln in out.splitlines() if "outer" in ln)
    inner_line = next(ln for ln in out.splitlines() if "inner" in ln)
    assert len(inner_line) - len(inner_line.lstrip()) > len(outer_line) - len(
        outer_line.lstrip()
    )


def test_a_task_draws_no_duplicate_source_bar(task_rig):
    """Task events are ordinary records located at the `task()` call line, so
    without the store-side filter every named bar would get a pulsing
    source-location bar drawn beside it."""
    with lumberjack.task("reindex", total=10) as t:
        for _ in range(5):
            t.advance()
    task_rig.tick()
    assert "reindex" in _strip_ansi(task_rig.output())
    # The source model is the observable contract here: a substring check on
    # the frame is defeated by column truncation at narrow widths.
    assert task_rig.renderer.bars() == [], "the task's own call site got a bar"


def test_named_bars_are_drawn_above_source_bars(task_rig):
    """The overflow ellipsis crops from the bottom, so exact bars must not
    lose their slots to inferred ones."""
    # A label that cannot occur in this test's own source-bar text, which is
    # labelled with the enclosing function's name.
    with lumberjack.task("zzexact", total=10) as t:
        t.set_progress(5)
        for i in range(5):
            task_rig.logger.info("loop line %d", i)
    task_rig.tick()
    # One clean frame: the stream accumulates every redraw, and an early frame
    # drawn before the source bar qualified would satisfy any ordering.
    task_rig.stream.seek(0)
    task_rig.stream.truncate(0)
    task_rig.tick()
    frame = _strip_ansi(task_rig.output())
    assert "zzexact" in frame and "records" in frame
    assert frame.index("zzexact") < frame.index("records"), "source bars drew first"


def test_a_task_that_overshoots_its_total_goes_back_to_pulsing(task_rig):
    """rich clamps `completed > total` to a full 100% bar, which reads as
    "finished" while the work is still running. Withdrawing the claim is the
    honest degradation; the count column still shows the real numbers."""
    with lumberjack.task("underestimated", total=10) as t:
        t.set_progress(25)
        task_rig.tick()
    frame = _strip_ansi(task_rig.output())
    line = next(ln for ln in frame.splitlines() if "underestimated" in ln)
    assert "25/10" in line, "the real numbers must stay visible"
    assert "100%" not in line, "an overshooting task must not read as finished"
    assert "%" not in line, "and must not claim a percentage at all"


# --- inferred structure on screen ------------------------------------------
#
# The model decides all of this; what these pin is that the display says what
# the model concluded, and says nothing it did not conclude.


def _nested_frame(store: RecordStore, make_row, *, polls: int) -> str:
    """Draw an outer loop on line 4 with an inner loop of eight on line 6."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    # Ending flush against "now", so nothing has been quiet long enough to
    # retire — retirement is its own test below and would mask this one.
    at = time.time() - ((polls - 1) * 8.0 + 7.0)
    try:
        for _ in range(polls):
            rows = [make_row(lineno=4, func_name="outer", created=at)]
            rows += [
                make_row(lineno=6, func_name="inner", created=at + i) for i in range(8)
            ]
            store.append(rows)
            at += 8.0
            renderer.refresh()
        return _strip_ansi(stream.getvalue())
    finally:
        renderer.close()


def _line(frame: str, needle: str) -> str:
    """The needle's line as the *last* frame drew it.

    A live display rewrites in place, so the captured stream holds every frame
    since the first, separated by carriage returns as well as newlines. The
    interesting one is always the most recent.
    """
    return next(ln for ln in reversed(re.split(r"[\r\n]", frame)) if needle in ln)


def test_an_inferred_inner_loop_is_indented_under_its_parent(
    as_terminal, store: RecordStore, make_row
):
    frame = _nested_frame(store, make_row, polls=5)
    outer, inner = _line(frame, "foo.py:4"), _line(frame, "foo.py:6")
    assert inner.index("foo.py:6") > outer.index("foo.py:4"), "the inner bar sits flush"


def test_an_inferred_inner_loop_shows_its_position_in_the_cycle(
    as_terminal, store: RecordStore, make_row
):
    """The cycle position is the inferred half and the cumulative count is the
    certain one, so both are shown — a reader can see the guess beside the
    fact it was made from."""
    frame = _nested_frame(store, make_row, polls=5)
    assert "/8 · " in _line(frame, "foo.py:6")


def test_an_outermost_loop_never_claims_a_cycle(
    as_terminal, store: RecordStore, make_row
):
    """Nothing encloses it, so nothing says how long it is. Pulsing forever is
    the correct rendering rather than a missing feature."""
    frame = _nested_frame(store, make_row, polls=5)
    line = _line(frame, "foo.py:4")
    assert "·" not in line and "records" in line


def test_nothing_is_claimed_before_the_inference_settles(
    as_terminal, store: RecordStore, make_row
):
    frame = _nested_frame(store, make_row, polls=1)
    assert "·" not in frame, "a total was drawn on first sight"


def test_a_loop_that_went_quiet_says_so(as_terminal, store: RecordStore, make_row):
    """ "idle" rather than "done", because idleness is what was measured — no
    log line announces the end of a loop."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row(created=100.0 + i) for i in range(5)])
        renderer.refresh()
        assert "idle" in _line(_strip_ansi(stream.getvalue()), "foo.py:10")
    finally:
        renderer.close()


def test_a_loop_still_running_shows_its_rate_instead(
    as_terminal, store: RecordStore, make_row
):
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    now = time.time()
    try:
        store.append([make_row(created=now - 0.4 + i * 0.1) for i in range(5)])
        renderer.refresh()
        line = _line(_strip_ansi(stream.getvalue()), "foo.py:10")
        assert "10/s" in line and "idle" not in line
    finally:
        renderer.close()


def test_an_untimed_loop_shows_no_rate_at_all(
    as_terminal, store: RecordStore, make_row
):
    """Every record inside one clock tick, so there is no interval to report.
    Blank rather than "0/s" or "∞/s": a made-up number is worse than none."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row(created=100.0) for _ in range(5)])
        renderer.refresh()
        line = _line(_strip_ansi(stream.getvalue()), "foo.py:10")
        assert "/s" not in line and "idle" not in line
        assert "5 records" in line
    finally:
        renderer.close()


def test_the_live_display_leaves_stdout_alone(
    as_terminal, store: RecordStore, make_row
):
    """rich redirects both streams by default, and `Live.start()` would swap
    `sys.stdout` for a proxy writing to *this renderer's* stderr console. A
    program run as `app.py > data.txt` would then print its results to the
    terminal and write an empty file. lumberjack owns stderr; the channel a
    program uses for its output is not ours to move.

    Needs a terminal: rich only redirects either stream when the console is
    one, so without `as_terminal` this passes whatever the setting is.
    """
    real_stdout = sys.stdout
    renderer = RichProgressRenderer(
        store, stream=io.StringIO(), min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row() for _ in range(5)])
        renderer.refresh()
        assert sys.stdout is real_stdout
    finally:
        renderer.close()
    assert sys.stdout is real_stdout


def test_the_live_display_does_route_stderr(as_terminal, store: RecordStore):
    """The other half of the decision, deliberately left as rich's default: a
    raw `sys.stderr.write` mid-frame corrupts it, and routing it through the
    console prints it cleanly above the bars instead."""
    real_stderr = sys.stderr
    renderer = RichProgressRenderer(
        store, stream=io.StringIO(), min_repeats=3, refresh_interval=0
    )
    try:
        assert sys.stderr is not real_stderr, "stderr was left unrouted"
    finally:
        renderer.close()
    assert sys.stderr is real_stderr, "stderr was not handed back"
