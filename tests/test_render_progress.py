"""The premise on screen: log lines become bars, and the bars tell the truth.

These tests drive the real path — stdlib `logging` → `LumberjackHandler` →
`RecordStore` → bar — rather than poking the renderer directly, because the
premise being validated is end-to-end. What they cover, in order: a repeating
line collapsing into one advancing bar, the opt-in ceiling, what a lossy
display must not swallow, the redraw timer, teardown, named bars from the
tracking API, and the structure inferred for the rest.

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
import lumberjack.tracking  # noqa: E402
from lumberjack import (
    session,  # noqa: E402
    teardown,  # noqa: E402
)
from lumberjack.detect import OutputMode  # noqa: E402
from lumberjack.handler import LumberjackHandler  # noqa: E402
from lumberjack.renderers.progress import LoopRowModel  # noqa: E402
from lumberjack.renderers.rich_renderer import (  # noqa: E402
    RichProgressRenderer,
    _relayout,
)
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
        # legacy_windows pinned off: on a Windows runner rich detects it and
        # swaps its own `━` for `-`, so an assertion about a bar's shape would
        # fail there for a reason that has nothing to do with the display.
        # What rich does on a legacy console has its own tests.
        lambda **kwargs: real_console(
            force_terminal=True, width=100, legacy_windows=False, **kwargs
        ),
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


def test_four_call_sites_in_one_loop_body_draw_one_row(rig: _Rig):
    """`examples/demo.py siblings` in miniature, and the whole of #8.

    Four lines narrating one loop are one loop. Source location is the right
    *identity* for them and the wrong *display unit*, and the number a person
    wants is the 40 rows the code worked through rather than the 160 log calls
    that described them.
    """
    for row in range(40):
        rig.logger.info("row %d: parsed", row)
        rig.logger.info("row %d: schema validated", row)
        rig.logger.info("row %d: enriched from cache", row)
        rig.logger.info("row %d: emitted downstream", row)
    rig.tick()

    (loop,) = rig.renderer.rows()
    assert loop.count == 40, "the row counted records rather than iterations"
    assert len(loop.members) == 4
    # And the identity layer is untouched by any of it: four call sites, 160
    # records, which is what the store and the exit summary report.
    assert len(rig.renderer.bars()) == 4
    assert sum(bar.count for bar in rig.renderer.bars()) == 160


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


def _unrelated_loops(store: RecordStore, make_row, count: int, *, records: int = 3):
    """`count` separate loops, each pacing itself differently.

    Distinct periods are load-bearing here, not decoration. `/tmp/foo.py` has
    no source on disk, so the row model falls back to the runtime signal —
    equal periods plus a shared worker means one loop body — and sources that
    all fire within the same microsecond satisfy that and merge into one row.
    Correctly, and not what a test about the ceiling is asking.
    """
    for lineno in range(count):
        step = lineno + 1
        store.append(
            [make_row(lineno=lineno, created=100.0 + i * step) for i in range(records)]
        )


def test_the_ceiling_bounds_what_is_drawn(store: RecordStore, make_row):
    renderer = _capped_renderer(store, max_bars=2)
    try:
        _unrelated_loops(store, make_row, 5)
        renderer.refresh()
        assert len(renderer._tasks) == 2, "drew more bars than the ceiling allows"
    finally:
        renderer.close()


def test_the_ceiling_does_not_touch_the_counts(store: RecordStore, make_row):
    # The model tracks every source regardless; the ceiling is a property of
    # the display. A capped run must not misreport what it captured.
    renderer = _capped_renderer(store, max_bars=2)
    try:
        _unrelated_loops(store, make_row, 5)
        renderer.refresh()
        assert len(renderer.bars()) == 5
        assert sum(b.count for b in renderer.bars()) == 15
    finally:
        renderer.close()


def test_the_ceiling_counts_what_it_hid(store: RecordStore, make_row):
    renderer = _capped_renderer(store, max_bars=2)
    try:
        assert renderer.suppressed_bars == 0
        _unrelated_loops(store, make_row, 5)
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
        _unrelated_loops(store, make_row, 5)
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
        _unrelated_loops(store, make_row, 4)
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
        store.append([make_row(msg="fetched row %d") for _ in range(5)])
        renderer.refresh()
        frame = _strip_ansi(stream.getvalue())
        # The label is the stored template with its format specifiers
        # substituted — a description of what the line does, rather than the
        # file and line number it lives at (#56).
        assert "fetched row …" in frame
        # Iterations of the loop, not records captured (#8).
        assert "5 iterations" in frame
    finally:
        renderer.close()


def test_a_row_with_no_usable_template_falls_back_to_the_source_location(
    as_terminal, store: RecordStore, make_row
):
    """`file:line func()` is what shipped before and is still the answer where
    a template is missing — an empty `msg`, or one that survives to runtime as
    a rendered f-string and so matches nothing. Nothing regresses."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row(msg="") for _ in range(5)])
        renderer.refresh()
        assert "foo.py:10 bar()" in _strip_ansi(stream.getvalue())
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
    would be the one thing a bar must never do.

    Drawn *while the task is open*: once it ends, filling the bar is correct
    and is what the test below pins.
    """
    with lumberjack.task("scan") as t:
        t.advance()
        task_rig.tick()
        out = _strip_ansi(task_rig.output())
    assert "scan" in out
    assert "%" not in out, "an indeterminate task must not show a percentage"


def test_a_task_that_ends_without_a_total_still_finishes(task_rig):
    """An `end` row is an exact completion signal — the only kind of bar here
    that has one. Leaving it pulsing would say "still working" about work that
    is provably over."""
    with lumberjack.task("scan") as t:
        t.advance()
    task_rig.tick()
    line = _line(_strip_ansi(task_rig.output()), "scan")
    assert "100%" in line, "an ended task was left pulsing"


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
    assert "zzexact" in frame and "iterations" in frame
    assert frame.index("zzexact") < frame.index("iterations"), "source bars drew first"


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


#: What the two rows in `_nested_frame` are labelled, which is their message
#: template with the format specifier substituted (#56).
_OUTER, _INNER = "outer batch …", "inner row …"


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
            rows = [
                make_row(lineno=4, func_name="outer", msg="outer batch %d", created=at)
            ]
            rows += [
                make_row(
                    lineno=6,
                    func_name="inner",
                    msg="inner row %d",
                    created=at + i,
                )
                for i in range(8)
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
    outer, inner = _line(frame, _OUTER), _line(frame, _INNER)
    assert inner.index(_INNER) > outer.index(_OUTER), "the inner bar sits flush"


# --- where a row is drawn (#43) --------------------------------------------
#
# Two things have to hold: rich really does render in insertion order and
# survive being reordered underneath it, and the renderer really does put a
# child under the parent it was inferred to run inside. The first is a claim
# about somebody else's library, so it is pinned separately — an upgrade that
# changes it must fail here rather than silently scrambling the display.


def test_rich_renders_progress_tasks_in_insertion_order():
    """The mechanism the re-layout rests on. `Progress.tasks` is
    `list(self._tasks.values())` over a plain dict, so the dict's order is the
    screen's order and rebuilding it moves rows."""
    progress = rich_renderer_module.Progress()
    first = progress.add_task("first")
    second = progress.add_task("second")
    assert [task.id for task in progress.tasks] == [first, second]

    with progress._lock:
        progress._tasks = {tid: progress._tasks[tid] for tid in (second, first)}
    assert [task.id for task in progress.tasks] == [second, first]


def test_reordering_rich_tasks_preserves_their_state():
    """Why it is a reorder and not a remove-and-re-add: the `Task` carries the
    elapsed clock and the completion, and recreating it throws both away."""
    progress = rich_renderer_module.Progress()
    first = progress.add_task("first", total=10)
    second = progress.add_task("second", total=10)
    progress.update(first, completed=7)
    before = progress._tasks[first]

    _relayout(progress, [second, first])

    after = progress._tasks[first]
    assert after is before, "the Task was recreated rather than moved"
    assert after.completed == 7
    assert after.start_time == before.start_time
    assert [task.id for task in progress.tasks] == [second, first]


def test_relayout_keeps_rows_the_caller_did_not_mention():
    """A row missing from the order is a caller that stopped drawing it — the
    bar ceiling does exactly that — and losing it here would delete work from
    the screen for a reason that has nothing to do with structure."""
    progress = rich_renderer_module.Progress()
    first = progress.add_task("first")
    second = progress.add_task("second")
    _relayout(progress, [second])
    assert [task.id for task in progress.tasks] == [second, first]


def test_a_nested_bar_moves_under_its_parent_when_containment_settles(
    as_terminal, store: RecordStore, make_row
):
    """The defect this exists to fix. An inner loop logs N times per outer
    iteration, so it always qualifies for a bar first — and under
    first-qualified placement it then indents beneath whatever unrelated row
    happened to precede it. Here that is a loop on another thread, which is
    exactly the `pipeline` scenario.
    """
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    at = time.time() - 45.0
    try:
        for _ in range(6):
            # An unrelated loop on thread 2, fast enough to qualify on the
            # first poll and so to be registered before either of the others.
            rows = [
                make_row(
                    lineno=9,
                    func_name="other",
                    msg="elsewhere %d",
                    thread=2,
                    created=at + i * 0.5,
                )
                for i in range(16)
            ]
            rows.append(
                make_row(
                    lineno=4,
                    func_name="outer",
                    msg="outer batch %d",
                    thread=1,
                    created=at,
                )
            )
            rows += [
                make_row(
                    lineno=6,
                    func_name="inner",
                    msg="inner row %d",
                    thread=1,
                    created=at + i,
                )
                for i in range(8)
            ]
            store.append(rows)
            at += 8.0
            renderer.refresh()
        frame = _strip_ansi(stream.getvalue())
    finally:
        renderer.close()

    labels = ("elsewhere …", _OUTER, _INNER)
    drawn = [ln for ln in re.split(r"[\r\n]", frame) if any(x in ln for x in labels)]
    last = [next(x for x in labels if x in ln) for ln in drawn[-3:]]
    assert last == list(
        labels
    ), f"the child did not move under its parent: {drawn[-3:]}"


def test_a_rows_clock_survives_growth_a_move_and_a_collapse(
    as_terminal, store: RecordStore, make_row
):
    """Every way a row can change, in one sequence, against one `Task`.

    A row's key is frozen at its first sighting and never migrates, because
    the key is what the rich `Task` is filed under — and a recreated `Task`
    silently restarts the elapsed clock of a loop that has been running for
    ten minutes. Three things could have forced a recreation and none may:
    a sibling call site joining the row, the row moving on screen, and the row
    collapsing when it goes quiet.
    """
    clock = [1_000.0]
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    # The renderer's own model, rebuilt against a clock this test drives:
    # retirement is measured against wall time and the sequence has to cross
    # it. Nothing else about the renderer changes.
    renderer._model = LoopRowModel(store, min_repeats=3, clock=lambda: clock[0])
    try:
        store.append(
            [make_row(lineno=6, msg="parsed %d", created=990.0 + i) for i in range(4)]
        )
        renderer.refresh()
        (task_id,) = renderer._tasks.values()
        task = renderer._source_progress._tasks[task_id]
        started = task.start_time

        # A sibling call site at the same pace joins the row.
        store.append(
            [
                make_row(lineno=7, msg="validated %d", created=994.0 + i)
                for i in range(4)
            ]
        )
        renderer.refresh()
        (row,) = renderer.rows()
        assert len(row.members) == 2, "the sibling did not merge"
        assert len(renderer._tasks) == 1
        assert renderer._tasks[row.key] == task_id, "the row's key migrated"

        # An unrelated loop on another thread appears, and the first row goes
        # quiet: a live subtree sorts above a collapsed one, so the row moves.
        clock[0] = 1_050.0
        store.append(
            [
                make_row(lineno=9, thread=2, msg="elsewhere %d", created=1_046.5 + i)
                for i in range(4)
            ]
        )
        renderer.refresh()
        assert [r.idle for r in renderer.rows()] == [False, True]
        assert renderer._source_progress.tasks[-1].id == task_id, "the row did not move"

        assert renderer._source_progress._tasks[task_id] is task, "the Task was rebuilt"
        assert task.start_time == started, "the elapsed clock restarted"
        # Relabelled by the merge — a row covering two call sites is named for
        # the loop, not for either template — and now collapsed.
        line = _line(_strip_ansi(stream.getvalue()), "foo.py bar()")
        assert "━" not in line and "idle" in line
    finally:
        renderer.close()


def test_an_inferred_inner_loop_shows_its_position_in_the_cycle(
    as_terminal, store: RecordStore, make_row
):
    """The cycle position is the inferred half and the cumulative count is the
    certain one, so both are shown — a reader can see the guess beside the
    fact it was made from."""
    frame = _nested_frame(store, make_row, polls=5)
    assert "/8 · " in _line(frame, _INNER)


def test_an_outermost_loop_never_claims_a_cycle(
    as_terminal, store: RecordStore, make_row
):
    """Nothing encloses it, so nothing says how long it is. Pulsing forever is
    the correct rendering rather than a missing feature."""
    frame = _nested_frame(store, make_row, polls=5)
    line = _line(frame, _OUTER)
    assert "·" not in line and "iterations" in line


def test_nothing_is_claimed_before_the_inference_settles(
    as_terminal, store: RecordStore, make_row
):
    # Scoped to the bar row: the session heartbeat separates its count from
    # its rate with the same "·", and it is not what this is about. One poll
    # in, only the inner line has repeated often enough to have a bar at all.
    frame = _nested_frame(store, make_row, polls=1)
    assert "·" not in _line(frame, _INNER), "a total was drawn on first sight"


def test_a_loop_that_went_quiet_says_so(as_terminal, store: RecordStore, make_row):
    """ "idle" rather than "done", because idleness is what was measured — no
    log line announces the end of a loop."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row(msg="working %d", created=100.0 + i) for i in range(5)])
        renderer.refresh()
        assert "idle" in _line(_strip_ansi(stream.getvalue()), "working …")
    finally:
        renderer.close()


def test_a_collapsed_row_draws_no_bar(as_terminal, store: RecordStore, make_row):
    """A retired row stays — deleting it would empty the final frame — but it
    stops holding forty columns of finished bar for work that ended. Once
    every row has gone quiet the whole column is one character wide, which is
    the frame a finished run leaves behind."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row(msg="working %d", created=100.0 + i) for i in range(5)])
        renderer.refresh()
        line = _line(_strip_ansi(stream.getvalue()), "working …")
        assert "━" not in line, "a collapsed row still drew a full-width bar"
        assert "5 iterations" in line, "collapsing must not hide what it counted"
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
        store.append(
            [make_row(msg="working %d", created=now - 0.4 + i * 0.1) for i in range(5)]
        )
        renderer.refresh()
        line = _line(_strip_ansi(stream.getvalue()), "working …")
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
        store.append([make_row(msg="working %d", created=100.0) for _ in range(5)])
        renderer.refresh()
        line = _line(_strip_ansi(stream.getvalue()), "working …")
        assert "/s" not in line and "idle" not in line
        assert "5 iterations" in line
    finally:
        renderer.close()


# --- the second row a slow loop earns (#53) --------------------------------
#
# The model decides whether the row exists at all and what it points at (see
# `test_progress_position.py`); these pin that the display draws it, draws it
# under the loop it belongs to, and does not draw it for a loop that never
# earned one.

_SEQUENCE_SOURCE = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        log.debug("batch %d: fetching manifest", batch)
        log.debug("batch %d: committing", batch)
"""

_SEQUENCE_STAGES = (
    "batch %d: opening connection",
    "batch %d: fetching manifest",
    "batch %d: committing",
)

#: The loop statement in `_SEQUENCE_SOURCE`, which is what a merged row is
#: named for, and its three call sites.
_SEQUENCE_LOOP = "sequence.py:7 run()"
_SEQUENCE_FIRST_LINE = 8


def _sequence_frame(store, make_row, tmp_path, *, pace, cycles=4, upto=3, at=None):
    """One slow loop narrating three stages, drawn once and captured.

    A single `refresh()`, so the captured stream is one frame and the order of
    its lines is the order they were on screen — which is what the adjacency
    assertion below needs and what `_line`'s reverse search cannot give.
    """
    path = tmp_path / "sequence.py"
    path.write_text(_SEQUENCE_SOURCE, encoding="utf-8")
    start = time.time() - cycles * pace if at is None else at
    step = pace / len(_SEQUENCE_STAGES)
    for cycle in range(cycles):
        store.append(
            [
                make_row(
                    pathname=str(path),
                    lineno=_SEQUENCE_FIRST_LINE + index,
                    func_name="run",
                    msg=template,
                    created=start + cycle * pace + index * step,
                )
                for index, template in enumerate(_SEQUENCE_STAGES[:upto])
            ]
        )
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        renderer.refresh()
        return _strip_ansi(stream.getvalue()), renderer.rows()
    finally:
        renderer.close()


def _frame_lines(frame: str) -> list[str]:
    return [line for line in re.split(r"[\r\n]", frame) if line.strip()]


def test_a_loop_too_slow_to_read_draws_a_second_determinate_row(
    as_terminal, store: RecordStore, make_row, tmp_path
):
    """`sequence`. The loop row pulses — nothing bounds a `for` over a range
    the display cannot see — and beneath it the stage the body has reached is
    a real bar, because the AST says how many stages there are."""
    frame, rows = _sequence_frame(store, make_row, tmp_path, pace=3.0)
    (row,) = rows
    assert row.total is None, "the loop row claimed a total it cannot have"

    lines = _frame_lines(frame)
    loop = next(i for i, line in enumerate(lines) if _SEQUENCE_LOOP in line)
    assert "3 of 3" in lines[loop + 1], "no position row directly under the loop"
    assert "batch …: committing" in lines[loop + 1]


def test_the_position_row_is_indented_under_its_loop(
    as_terminal, store: RecordStore, make_row, tmp_path
):
    frame, _ = _sequence_frame(store, make_row, tmp_path, pace=3.0)
    lines = _frame_lines(frame)
    loop = next(i for i, line in enumerate(lines) if _SEQUENCE_LOOP in line)
    position = lines[loop + 1]
    assert position.index("batch") > lines[loop].index("sequence.py")


def test_the_position_row_fills_only_as_far_as_the_stage_reached(
    as_terminal, store: RecordStore, make_row, tmp_path
):
    """Determinate from the first frame, and partial: `1 of 3` is a third of a
    bar. Nothing here is inferred, so there is no pulse-then-promote."""
    frame, _ = _sequence_frame(store, make_row, tmp_path, pace=3.0, upto=1)
    line = _line(frame, "batch …: opening connection")
    assert "1 of 3" in line
    assert "━" in line and "╺" in line, "a determinate bar drawn full or empty"


def test_a_fast_loop_draws_no_second_row(
    as_terminal, store: RecordStore, make_row, tmp_path
):
    """`siblings`. Same file, same body, same merge — twenty iterations a
    second, and a sub-iteration bar there would show whichever of them the
    poll happened to land in."""
    frame, rows = _sequence_frame(store, make_row, tmp_path, pace=0.05, cycles=20)
    (row,) = rows
    assert row.position is None
    # The heartbeat and the loop row, and nothing else: a stage name on screen
    # would mean a position row got drawn.
    assert [line for line in _frame_lines(frame) if "batch" in line] == []


def test_a_position_row_collapses_with_the_loop_above_it(
    as_terminal, store: RecordStore, make_row, tmp_path
):
    """A finished loop's last stage is worth keeping — it says where the work
    stopped — but it should stop shouting, exactly as the bar above it does."""
    frame, _ = _sequence_frame(store, make_row, tmp_path, pace=3.0, at=100.0)
    line = _line(frame, "batch …: committing")
    assert "━" not in line, "a collapsed position row still drew a full-width bar"
    assert "3 of 3" in line, "collapsing must not hide where the work stopped"


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


def test_a_task_drawn_determinate_then_overshooting_withdraws_its_claim(
    task_rig, monkeypatch
):
    """The overshoot case that matters, and the one an earlier test missed.
    A bar created *already* overshot is indeterminate from birth, which
    `add_task(total=None)` gives for free. A bar drawn determinate first has
    to have the total taken back off it — and `Progress.update(total=None)`
    means "leave the total alone", so that path was silently a no-op and rich
    clamped the stale total to a finished-looking 100%.

    Ticks are sampled at one per 50ms, and this needs two updates in the same
    breath, so sampling is switched off rather than slept through.
    """
    monkeypatch.setattr(lumberjack.tracking, "TICK_INTERVAL", 0.0)
    with lumberjack.task("underestimated", total=10) as t:
        t.set_progress(5)
        task_rig.tick()
        assert "50%" in _line(_strip_ansi(task_rig.output()), "underestimated")
        t.set_progress(25)
        task_rig.tick()
        line = _line(_strip_ansi(task_rig.output()), "underestimated")
    assert "25/10" in line, "the real numbers must stay visible"
    assert "%" not in line, "the withdrawn claim came back as a full bar"


def test_a_retired_source_bar_that_resumes_stops_claiming_completion(
    as_terminal, store: RecordStore, make_row
):
    """Filling an idle bar implies it finished. If the loop turns out to be
    alive after all, that total has to come back off — the same withdrawal
    the overshoot case needs, from the other direction."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    try:
        store.append([make_row(msg="working %d", created=100.0 + i) for i in range(5)])
        renderer.refresh()
        assert "idle" in _line(_strip_ansi(stream.getvalue()), "working …")

        now = time.time()
        store.append(
            [make_row(msg="working %d", created=now - 0.4 + i * 0.1) for i in range(5)]
        )
        renderer.refresh()
        line = _line(_strip_ansi(stream.getvalue()), "working …")
        assert "idle" not in line
        assert "%" not in line, "a resumed bar kept the total that retiring gave it"
    finally:
        renderer.close()


# --- the session heartbeat --------------------------------------------------
#
# The row for a program whose log lines never repeat, which before this drew
# nothing at all. The two shapes it exists for are `examples/demo.py oneshot`
# (six startup lines, one each) and `silent` (a line, three seconds of real
# work, a line).


def _heartbeat_line(rig: _Rig) -> str:
    """The heartbeat row as the last frame drew it.

    Found by the event count rather than by the glyph, which is the thing
    under test in half of these — a helper keyed on a particular frame would
    quietly stop finding the row the moment the beat moved.
    """
    return _line(_strip_ansi(rig.output()), " event")


def _every_heartbeat(rig: _Rig) -> list[str]:
    """The heartbeat row as *every* frame so far drew it, oldest first."""
    lines = re.split(r"[\r\n]", _strip_ansi(rig.output()))
    return [line.rstrip() for line in lines if " event" in line]


@pytest.fixture
def live(as_terminal: None, store: RecordStore) -> Iterator[_Rig]:
    """A renderer drawing to a terminal, driven a refresh at a time."""
    stream = io.StringIO()
    renderer = RichProgressRenderer(
        store, stream=stream, min_repeats=3, refresh_interval=0
    )
    handler = LumberjackHandler(on_record=renderer.render, level=logging.DEBUG)
    logger = logging.getLogger("heartbeat-rig")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield _Rig(logger, handler, store, renderer, stream)
    finally:
        logger.removeHandler(handler)
        renderer.close()


def test_a_program_whose_lines_never_repeat_still_draws_something(live: _Rig, make_row):
    """`oneshot`. Six sources, one record each: no source repeats, so no
    source earns a bar, and the display was empty — indistinguishable from
    hung, which is the first question it exists to answer."""
    for i, message in enumerate(
        (
            "reading configuration from /etc/pipeline.toml",
            "connecting to warehouse at db.internal:5432",
            "negotiated protocol version 3",
            "warming schema cache",
            "registered 14 table mappings",
            "ready",
        )
    ):
        live.store.append([make_row(lineno=200 + i, message=message)])
        live.renderer.refresh()

    assert live.renderer.bars() == [], "a one-off line must not earn a bar"
    line = _heartbeat_line(live)
    assert "6 events" in line
    assert "ready" in line


def test_the_heartbeat_stops_when_the_records_stop(live: _Rig, make_row):
    """`silent`. Three seconds of real work between two lines, and nothing in
    the stream to see. A stopped heartbeat is the truthful frame — a frame
    turning on wall-clock time would be claiming liveness nobody observed."""
    live.store.append([make_row(message="rendering 2.4M points at dpi=200")])
    for _ in range(15):  # the silence, one redraw at a time
        live.renderer.refresh()

    drawn = set(_every_heartbeat(live))
    assert len(drawn) == 1, f"the heartbeat moved while nothing arrived: {drawn}"

    live.store.append([make_row(message="wrote figure.png")])
    live.renderer.refresh()
    assert "wrote figure.png" in _heartbeat_line(live)


def test_the_heartbeat_keeps_no_clock(live: _Rig, make_row):
    """An elapsed counter would tick through the silence above, which is the
    same lie in a different column."""
    live.store.append([make_row() for _ in range(4)])
    live.renderer.refresh()
    assert not re.search(r"\d+:\d\d:\d\d", _heartbeat_line(live))


def test_nothing_captured_draws_no_heartbeat_row(live: _Rig):
    live.renderer.refresh()
    assert live.output() == "" or "event" not in _strip_ansi(live.output())


def test_the_heartbeat_is_drawn_above_every_bar(live: _Rig, make_row):
    """The overflow ellipsis crops from the bottom, and liveness is the row
    worth keeping."""
    live.store.append([make_row(message=f"fetched row {i}") for i in range(5)])
    live.renderer.refresh()
    live.stream.seek(0)
    live.stream.truncate(0)
    live.renderer.refresh()

    frame = _strip_ansi(live.output())
    assert "iterations" in frame, "no source bar was drawn to sit under"
    assert frame.index("events") < frame.index("iterations")


def test_the_newest_line_is_echoed_once_rather_than_scrolled(live: _Rig):
    """The premise, restated for the row that carries a message: fifty log
    lines still produce one row, and it holds the newest of them rather than
    all fifty.

    Counted on the *rendered* lines rather than on the template, because the
    loop's bar is now labelled with the template itself (#56) — which is a
    string none of the fifty records carries.
    """
    for i in range(50):
        live.logger.info("processing item %d", i)
    live.tick()

    echoed = [
        line
        for line in re.split(r"[\r\n]", _strip_ansi(live.output()))
        if re.search(r"processing item \d", line)
    ]
    assert echoed, "the newest line is not shown anywhere"
    assert all(" event" in line for line in echoed), "the loop's lines scrolled"
    assert all("processing item 49" in line for line in echoed)


def test_a_warning_is_not_echoed_by_the_row_that_summarises_it(live: _Rig):
    """It already printed above the bars, in full. Twice is once too many,
    and a one-off warning parked in the live row reads as the current state
    of the program."""
    for i in range(5):
        live.logger.info("processing item %d", i)
    live.logger.warning("disk is filling up")
    live.tick()

    assert _strip_ansi(live.output()).count("disk is filling up") == 1
    line = _heartbeat_line(live)
    assert "disk is filling up" not in line
    assert "processing item 4" in line, "the last collapsed line stands instead"
    assert "6 events" in line, "a warning is still a record that arrived"


def test_a_session_that_only_warns_shows_a_count_and_no_message(live: _Rig):
    """Every line printed above the bars in full, so there is nothing left
    for the row to echo. It keeps the count — records did arrive — and says
    nothing else rather than repeating one of them."""
    for i in range(3):
        live.logger.warning("disk is filling up (%d)", i)
    live.tick()

    line = _heartbeat_line(live)
    assert "3 events" in line
    assert "disk is filling up" not in line


# `TimeElapsedColumn` reads `Task.finished_time` and `Task.stop_time`, and at
# test timescales every frame renders `0:00:00` whatever they hold — so these
# two assert the fields rather than the text. They are attributes of rich's
# public `Task`, not lumberjack internals; what is reached through privately is
# only the renderer's handle on its own `Progress`.


def _task_bar(renderer: RichProgressRenderer):
    (task,) = renderer._task_progress.tasks
    return task


def test_an_ended_task_stops_its_clock(task_rig):
    """A bar that ended keeps counting elapsed time unless the task is
    stopped: rich only latches the clock when `completed >= total`, which an
    under-delivering or indeterminate task never reaches."""
    with lumberjack.task("scan") as t:
        t.advance()
        task_rig.tick()
        assert _task_bar(task_rig.renderer).stop_time is None
    task_rig.tick()
    assert _task_bar(task_rig.renderer).stop_time is not None, "the clock ran on"


def test_a_withdrawn_claim_unfreezes_the_clock(task_rig, monkeypatch):
    """rich latches `finished_time` the moment `completed >= total`, and
    `Task.elapsed` returns it forever after. A bar that briefly looked
    finished before its claim was withdrawn would keep a stopped clock while
    the work carried on."""
    monkeypatch.setattr(lumberjack.tracking, "TICK_INTERVAL", 0.0)
    with lumberjack.task("underestimated", total=10) as t:
        t.set_progress(10)
        task_rig.tick()
        assert _task_bar(task_rig.renderer).finished_time is not None
        t.set_progress(25)
        task_rig.tick()
        assert _task_bar(task_rig.renderer).finished_time is None, "clock stayed frozen"


def test_a_container_task_finishes_at_a_hundred_percent(task_rig):
    """A task that only ever held subtasks has no count of its own, so its
    total is 0 — and rich renders 0-of-0 as a full bar labelled 0%, which
    reads as a failure rather than as completion."""
    with lumberjack.task("etl run"):
        pass
    task_rig.tick()
    line = _line(_strip_ansi(task_rig.output()), "etl run")
    assert "100%" in line
    assert " 0%" not in line, "0 of 0 rendered as a full bar labelled 0%"


def test_a_task_that_ends_short_of_its_total_keeps_both_numbers(task_rig):
    """Stopping at 5 of a claimed 10 is a fact about the run. Filling the bar
    would overwrite it with a claim of 10, which is the opposite of what
    finishing a bar is supposed to communicate."""
    with lumberjack.task("gave up early", total=10) as t:
        t.set_progress(5)
    task_rig.tick()
    line = _line(_strip_ansi(task_rig.output()), "gave up early")
    assert "50%" in line and "5/10" in line


def test_a_legacy_windows_console_gets_glyphs_it_can_draw(store: RecordStore):
    """rich swaps its *own* bars for ASCII on a legacy console. Ours have to
    follow, or a row mixes rich's `-` with our `▪` and gets the worst of both.

    Regression test with a history: an earlier fix routed "degrade" through a
    `None` encoding, then `None` was given the opposite meaning — rich's own
    convention, "the stream did not say, assume utf-8" — and this branch went
    back to drawing braille on the one console that cannot take it. The two
    meanings are now said separately, and this pins both.
    """
    real_console = rich_renderer_module.Console

    def make(legacy: bool, monkeypatch: pytest.MonkeyPatch) -> RichProgressRenderer:
        monkeypatch.setattr(
            rich_renderer_module,
            "Console",
            lambda **kwargs: real_console(
                force_terminal=True, width=90, legacy_windows=legacy, **kwargs
            ),
        )
        return RichProgressRenderer(store, stream=io.StringIO(), refresh_interval=0)

    with pytest.MonkeyPatch.context() as patch:
        legacy, modern = make(True, patch), make(False, patch)
        try:
            assert legacy._frames.isascii(), "braille where it cannot be encoded"
            assert not modern._frames.isascii(), "ASCII dots on a console that can"
            marks = [
                next(
                    c
                    for c in r._source_progress.columns
                    if hasattr(c, "_collapsed_mark")
                )
                for r in (legacy, modern)
            ]
            assert marks[0]._collapsed_mark.isascii()
            assert not marks[1]._collapsed_mark.isascii()
        finally:
            legacy.close()
            modern.close()
