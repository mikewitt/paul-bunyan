"""The premise on screen: log lines become bars, and the bars tell the truth.

These tests drive the real path — stdlib `logging` → `LumberjackHandler` →
`RecordStore` → bar — rather than poking the renderer directly, because the
premise being validated is end-to-end. What they cover, in order: a repeating
line collapsing into one advancing bar, the opt-in ceiling, what a lossy
display must not swallow, the redraw timer, teardown, and the structure
inferred for the rest. Named bars from the tracking API are pinned on screen
in `test_render_tasks.py`.

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

import lumberjack.renderers.rich_renderer as rich_renderer_module
from fixture_sources import SEQUENCE, STAGES
from lumberjack.handler import LumberjackHandler
from lumberjack.renderers.progress import LoopRowModel
from lumberjack.renderers.rich_renderer import RichProgressRenderer
from lumberjack.store import RecordStore

_TIMER_THREAD = "lumberjack-progress"


def _timer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _TIMER_THREAD]


@pytest.fixture
def make_renderer(
    store: RecordStore,
) -> Iterator[Callable[..., RichProgressRenderer]]:
    """Construct, track and close every `RichProgressRenderer` a test builds.

    Every renderer built through the factory is closed automatically at
    teardown — closing is idempotent, so a test that also closes one itself
    (to observe the moment of closing) does not need its own try/finally.
    The stream stays the caller's, because most tests read it back directly.
    """
    created: list[RichProgressRenderer] = []

    def _make(
        *,
        min_repeats: int = 3,
        max_bars: int | None = None,
        refresh_interval: float = 0,
        stream: io.StringIO | None = None,
        **kwargs: object,
    ) -> RichProgressRenderer:
        renderer = RichProgressRenderer(
            store,
            stream=stream if stream is not None else io.StringIO(),
            min_repeats=min_repeats,
            refresh_interval=refresh_interval,
            max_bars=max_bars,
            **kwargs,  # type: ignore[arg-type]
        )
        created.append(renderer)
        return renderer

    yield _make
    for renderer in created:
        renderer.close()


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
def rig(
    store: RecordStore, make_renderer: Callable[..., RichProgressRenderer]
) -> Iterator[_Rig]:
    """A thin wrapper over `make_renderer`: the same tracked renderer, plus
    the logger/handler that feeds it so a test can log through the real
    capture path."""
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    handler = LumberjackHandler(on_record=renderer.render, level=logging.DEBUG)
    logger = logging.getLogger("progress-rig")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield _Rig(logger, handler, store, renderer, stream)
    finally:
        logger.removeHandler(handler)


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
    label = bar.source.format()
    assert label.startswith("test_render_progress.py:")
    assert label.endswith("test_a_loop_of_log_lines_becomes_one_advancing_bar()")


def test_the_loop_lines_never_scroll(rig: _Rig):
    for i in range(50):
        rig.logger.info("processing item %d", i)
    rig.tick()
    # The whole point: a thousand lines in, one bar out.
    assert "processing item" not in rig.output()


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


def _unrelated_loops(store: RecordStore, make_row, count: int, *, records: int = 3):
    """`count` separate loops, each pacing itself differently.

    Distinct periods are load-bearing here, not decoration. `/nonexistent/foo.py` has
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


def test_the_ceiling_does_not_touch_the_counts(
    store: RecordStore, make_row, make_renderer
):
    # The model tracks every source regardless; the ceiling is a property of
    # the display. A capped run must not misreport what it captured.
    renderer = make_renderer(max_bars=2)
    _unrelated_loops(store, make_row, 5)
    renderer.refresh()
    assert len(renderer.bars()) == 5
    assert sum(b.count for b in renderer.bars()) == 15


def test_the_ceiling_counts_what_it_hid(store: RecordStore, make_row, make_renderer):
    renderer = make_renderer(max_bars=2)
    assert renderer.suppressed_bars == 0
    _unrelated_loops(store, make_row, 5)
    renderer.refresh()
    assert renderer.suppressed_bars == 3


def test_no_ceiling_draws_everything(
    store: RecordStore, make_row, monkeypatch, make_renderer
):
    monkeypatch.delenv("LUMBERJACK_MAX_BARS", raising=False)
    renderer = make_renderer()
    _unrelated_loops(store, make_row, 5)
    renderer.refresh()
    assert renderer.suppressed_bars == 0


def test_the_environment_sets_the_ceiling(
    store: RecordStore, make_row, monkeypatch, make_renderer
):
    # The whole point of the knob: reachable without touching init().
    monkeypatch.setenv("LUMBERJACK_MAX_BARS", "1")
    renderer = make_renderer()
    _unrelated_loops(store, make_row, 4)
    renderer.refresh()
    assert renderer.suppressed_bars == 3


# --- lossy display, lossless store ----------------------------------------


def test_swallowed_records_are_still_in_the_store(rig: _Rig):
    for i in range(20):
        rig.logger.info("processing item %d", i)
    rig.tick()
    messages = [r.message for r in rig.store.recent()]
    assert len(messages) == 20
    assert messages[0] == "processing item 0"


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


def test_records_below_the_passthrough_level_are_collapsed(make_row, make_renderer):
    stream = io.StringIO()
    renderer = make_renderer(stream=stream, passthrough_level=logging.ERROR)
    renderer.render(make_row(level_name="WARNING", level_no=logging.WARNING))
    assert stream.getvalue() == ""
    renderer.render(make_row(level_name="ERROR", level_no=logging.ERROR))
    assert "hello world" in stream.getvalue()


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
    store: RecordStore, make_row, wait_until: Callable[..., bool], make_renderer
):
    renderer = make_renderer(refresh_interval=0.01)
    store.append([make_row() for _ in range(6)])
    assert wait_until(lambda: [b.count for b in renderer.bars()] == [6])


def test_a_zero_interval_starts_no_timer(make_renderer):
    make_renderer()
    assert _timer_threads() == []


# --- teardown --------------------------------------------------------------


def test_close_leaves_no_timer_thread_behind(make_renderer):
    renderer = make_renderer(refresh_interval=0.01)
    renderer.close()
    assert _timer_threads() == []


def test_close_is_idempotent(make_renderer):
    renderer = make_renderer()
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


def test_close_survives_a_store_that_closed_first(
    store: RecordStore, make_row, make_renderer
):
    store.append([make_row() for _ in range(5)])
    renderer = make_renderer()
    store.close()
    renderer.close()  # must not raise: the display still has to be handed back


def test_render_and_refresh_after_close_do_nothing(
    store: RecordStore, make_row, make_renderer
):
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    store.append([make_row() for _ in range(5)])
    renderer.close()
    before = stream.getvalue()
    renderer.refresh()
    renderer.render(make_row(level_name="ERROR", level_no=logging.ERROR))
    assert stream.getvalue() == before


def test_live_display_hides_then_restores_the_cursor(
    as_terminal, store: RecordStore, make_row, make_renderer
):
    # Never corrupt a traceback: the display must give the terminal back
    # before anything else prints.
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    store.append([make_row() for _ in range(5)])
    renderer.refresh()
    assert "\x1b[?25l" in stream.getvalue()  # cursor hidden while live
    renderer.close()
    assert stream.getvalue().endswith("\x1b[?25h")  # and handed back


def test_the_bar_is_actually_drawn_on_a_terminal(
    as_terminal, store: RecordStore, make_row, make_renderer, strip_ansi
):
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    store.append([make_row(msg="fetched row %d") for _ in range(5)])
    renderer.refresh()
    frame = strip_ansi(stream.getvalue())
    # The label is the stored template with its format specifiers
    # substituted — a description of what the line does, rather than the
    # file and line number it lives at (#56).
    assert "fetched row …" in frame
    # Iterations of the loop, not records captured (#8).
    assert "5 iterations" in frame


# --- degrades, never errors ------------------------------------------------


def test_raises_a_clear_error_without_rich(monkeypatch, make_renderer):
    # The factory never reaches this path without rich; it is the guard for
    # anyone constructing the renderer directly.
    monkeypatch.setattr(rich_renderer_module, "Progress", None)
    with pytest.raises(RuntimeError, match="rich is not installed"):
        make_renderer()


# --- inferred structure on screen ------------------------------------------
#
# The model decides all of this; what these pin is that the display says what
# the model concluded, and says nothing it did not conclude.


#: What the two rows in `_nested_frame` are labelled, which is their message
#: template with the format specifier substituted (#56).
_OUTER, _INNER = "outer batch …", "inner row …"


def _nested_frame(
    store: RecordStore, make_row, make_renderer, strip_ansi, *, polls: int
) -> str:
    """Draw an outer loop on line 4 with an inner loop of eight on line 6."""
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    # Ending flush against "now", so nothing has been quiet long enough to
    # retire — retirement is its own test below and would mask this one.
    at = time.time() - ((polls - 1) * 8.0 + 7.0)
    for _ in range(polls):
        rows = [make_row(lineno=4, func_name="outer", msg="outer batch %d", created=at)]
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
    return strip_ansi(stream.getvalue())


def _line(frame: str, needle: str) -> str:
    """The needle's line as the *last* frame drew it.

    A live display rewrites in place, so the captured stream holds every frame
    since the first, separated by carriage returns as well as newlines. The
    interesting one is always the most recent.
    """
    return next(ln for ln in reversed(re.split(r"[\r\n]", frame)) if needle in ln)


def test_a_nested_bar_moves_under_its_parent_when_containment_settles(
    as_terminal, store: RecordStore, make_row, make_renderer, strip_ansi
):
    """The defect this exists to fix. An inner loop logs N times per outer
    iteration, so it always qualifies for a bar first — and under
    first-qualified placement it then indents beneath whatever unrelated row
    happened to precede it. Here that is a loop on another thread, which is
    exactly the `pipeline` scenario.
    """
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    at = time.time() - 45.0
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
    frame = strip_ansi(stream.getvalue())

    labels = ("elsewhere …", _OUTER, _INNER)
    drawn = [ln for ln in re.split(r"[\r\n]", frame) if any(x in ln for x in labels)]
    last = [next(x for x in labels if x in ln) for ln in drawn[-3:]]
    assert last == list(
        labels
    ), f"the child did not move under its parent: {drawn[-3:]}"


def test_a_rows_clock_survives_growth_a_move_and_a_collapse(
    as_terminal, store: RecordStore, make_row, make_renderer, strip_ansi
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
    renderer = make_renderer(stream=stream)
    # The renderer's own model, rebuilt against a clock this test drives:
    # retirement is measured against wall time and the sequence has to cross
    # it. Nothing else about the renderer changes.
    renderer._model = LoopRowModel(store, min_repeats=3, clock=lambda: clock[0])
    store.append(
        [make_row(lineno=6, msg="parsed %d", created=990.0 + i) for i in range(4)]
    )
    renderer.refresh()
    (task_id,) = renderer._tasks.values()
    task = renderer._source_progress._tasks[task_id]
    started = task.start_time

    # A sibling call site at the same pace joins the row.
    store.append(
        [make_row(lineno=7, msg="validated %d", created=994.0 + i) for i in range(4)]
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
    line = _line(strip_ansi(stream.getvalue()), "foo.py bar()")
    assert "━" not in line and "idle" in line


def test_an_inferred_inner_loop_shows_its_position_in_the_cycle(
    as_terminal, store: RecordStore, make_row, make_renderer, strip_ansi
):
    """The cycle position is the inferred half and the cumulative count is the
    certain one, so both are shown — a reader can see the guess beside the
    fact it was made from."""
    frame = _nested_frame(store, make_row, make_renderer, strip_ansi, polls=5)
    assert "/8 · " in _line(frame, _INNER)


# --- collapse, rate, and resuming from either -------------------------------
#
# Once a row has gone quiet it stops holding a full-width bar for work that
# ended (collapse). While it is still moving it shows a rate, or nothing if
# no interval could be measured (rate). And a row that resumes after looking
# finished has to take that claim back (resume) — the same withdrawal the
# overshoot case needs, from the other direction.


def test_a_quiet_row_collapses_but_keeps_its_count(
    as_terminal, store: RecordStore, make_row, make_renderer, strip_ansi
):
    """ "idle" rather than "done", because idleness is what was measured — no
    log line announces the end of a loop. And a retired row stays — deleting
    it would empty the final frame that "drain before closing" exists to
    preserve — but it stops holding forty columns of finished bar."""
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    store.append([make_row(msg="working %d", created=100.0 + i) for i in range(5)])
    renderer.refresh()
    line = _line(strip_ansi(stream.getvalue()), "working …")
    assert "idle" in line
    assert "━" not in line, "a collapsed row still drew a full-width bar"
    assert "5 iterations" in line, "collapsing must not hide what it counted"


@pytest.mark.parametrize(
    ("created_offsets", "expects_rate"),
    [
        ([-0.4, -0.3, -0.2, -0.1, 0.0], True),  # spread out: a real interval
        ([0.0, 0.0, 0.0, 0.0, 0.0], False),  # one clock tick: no interval
    ],
)
def test_a_loops_rate_reflects_whether_an_interval_was_measured(
    as_terminal,
    store: RecordStore,
    make_row,
    make_renderer,
    strip_ansi,
    created_offsets,
    expects_rate,
):
    """A moving loop shows its rate instead of "idle"; an untimed one — every
    record inside one clock tick — shows neither, since a made-up number is
    worse than none."""
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    now = time.time() if expects_rate else 100.0
    store.append(
        [make_row(msg="working %d", created=now + offset) for offset in created_offsets]
    )
    renderer.refresh()
    line = _line(strip_ansi(stream.getvalue()), "working …")
    assert "idle" not in line
    if expects_rate:
        assert "10/s" in line
    else:
        assert "/s" not in line
        assert "5 iterations" in line


def test_a_retired_source_bar_that_resumes_stops_claiming_completion(
    as_terminal, store: RecordStore, make_row, make_renderer, strip_ansi
):
    """Filling an idle bar implies it finished. If the loop turns out to be
    alive after all, that total has to come back off — the same withdrawal
    the overshoot case needs, from the other direction."""
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    store.append([make_row(msg="working %d", created=100.0 + i) for i in range(5)])
    renderer.refresh()
    assert "idle" in _line(strip_ansi(stream.getvalue()), "working …")

    now = time.time()
    store.append(
        [make_row(msg="working %d", created=now - 0.4 + i * 0.1) for i in range(5)]
    )
    renderer.refresh()
    line = _line(strip_ansi(stream.getvalue()), "working …")
    assert "idle" not in line
    assert "%" not in line, "a resumed bar kept the total that retiring gave it"


# --- the second row a slow loop earns (#53) --------------------------------
#
# The model decides whether the row exists at all and what it points at (see
# `test_progress_position.py`); these pin that the display draws it, draws it
# under the loop it belongs to, and does not draw it for a loop that never
# earned one.

#: The loop statement in `SEQUENCE` (from `fixture_sources`), which is what a
#: merged row is named for, and its three call sites.
_SEQUENCE_LOOP = "sequence.py:7 run()"
_SEQUENCE_FIRST_LINE = 8


def _sequence_frame(
    store,
    make_row,
    make_renderer,
    write_module,
    strip_ansi,
    *,
    pace,
    cycles=4,
    upto=3,
    at=None,
):
    """One slow loop narrating three stages, drawn once and captured.

    A single `refresh()`, so the captured stream is one frame and the order of
    its lines is the order they were on screen — which is what the adjacency
    assertion below needs and what `_line`'s reverse search cannot give.
    """
    path = write_module(SEQUENCE, name="sequence.py", strip=False)
    start = time.time() - cycles * pace if at is None else at
    step = pace / len(STAGES)
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
                for index, template in enumerate(STAGES[:upto])
            ]
        )
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    renderer.refresh()
    return strip_ansi(stream.getvalue()), renderer.rows()


def _frame_lines(frame: str) -> list[str]:
    return [line for line in re.split(r"[\r\n]", frame) if line.strip()]


def test_a_loop_too_slow_to_read_draws_a_second_determinate_row(
    as_terminal, store: RecordStore, make_row, make_renderer, write_module, strip_ansi
):
    """`sequence`. The loop row pulses — nothing bounds a `for` over a range
    the display cannot see — and beneath it the stage the body has reached is
    a real bar, because the AST says how many stages there are. Indented
    under its loop, not flush with it."""
    frame, rows = _sequence_frame(
        store, make_row, make_renderer, write_module, strip_ansi, pace=3.0
    )
    (row,) = rows
    assert row.total is None, "the loop row claimed a total it cannot have"

    lines = _frame_lines(frame)
    loop = next(i for i, line in enumerate(lines) if _SEQUENCE_LOOP in line)
    position = lines[loop + 1]
    assert "3 of 3" in position, "no position row directly under the loop"
    assert "batch …: validating checksums" in position
    assert position.index("batch") > lines[loop].index(
        "sequence.py"
    ), "the position row is not indented under its loop"


def test_the_position_row_fills_only_as_far_as_the_stage_reached(
    as_terminal, store: RecordStore, make_row, make_renderer, write_module, strip_ansi
):
    """Determinate from the first frame, and partial: `1 of 3` is a third of a
    bar. Nothing here is inferred, so there is no pulse-then-promote."""
    frame, _ = _sequence_frame(
        store, make_row, make_renderer, write_module, strip_ansi, pace=3.0, upto=1
    )
    line = _line(frame, "batch …: opening connection")
    assert "1 of 3" in line
    assert "━" in line and "╺" in line, "a determinate bar drawn full or empty"


def test_a_fast_loop_draws_no_second_row(
    as_terminal, store: RecordStore, make_row, make_renderer, write_module, strip_ansi
):
    """`siblings`. Same file, same body, same merge — twenty iterations a
    second, and a sub-iteration bar there would show whichever of them the
    poll happened to land in."""
    frame, rows = _sequence_frame(
        store,
        make_row,
        make_renderer,
        write_module,
        strip_ansi,
        pace=0.05,
        cycles=20,
    )
    (row,) = rows
    assert row.position is None
    # The heartbeat and the loop row, and nothing else: a stage name on screen
    # would mean a position row got drawn.
    assert [line for line in _frame_lines(frame) if "batch" in line] == []


def test_a_position_row_collapses_with_the_loop_above_it(
    as_terminal, store: RecordStore, make_row, make_renderer, write_module, strip_ansi
):
    """A finished loop's last stage is worth keeping — it says where the work
    stopped — but it should stop shouting, exactly as the bar above it does."""
    frame, _ = _sequence_frame(
        store,
        make_row,
        make_renderer,
        write_module,
        strip_ansi,
        pace=3.0,
        at=100.0,
    )
    line = _line(frame, "batch …: validating checksums")
    assert "━" not in line, "a collapsed position row still drew a full-width bar"
    assert "3 of 3" in line, "collapsing must not hide where the work stopped"


def test_the_live_display_leaves_stdout_alone(
    as_terminal, store: RecordStore, make_row, make_renderer
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
    renderer = make_renderer()
    store.append([make_row() for _ in range(5)])
    renderer.refresh()
    assert sys.stdout is real_stdout
    renderer.close()
    assert sys.stdout is real_stdout


def test_the_live_display_does_route_stderr(as_terminal, make_renderer):
    """The other half of the decision, deliberately left as rich's default: a
    raw `sys.stderr.write` mid-frame corrupts it, and routing it through the
    console prints it cleanly above the bars instead."""
    real_stderr = sys.stderr
    renderer = make_renderer()
    assert sys.stderr is not real_stderr, "stderr was left unrouted"
    renderer.close()
    assert sys.stderr is real_stderr, "stderr was not handed back"


# --- the session heartbeat --------------------------------------------------
#
# The row for a program whose log lines never repeat, which before this drew
# nothing at all. The two shapes it exists for are `examples/demo.py oneshot`
# (six startup lines, one each) and `silent` (a line, three seconds of real
# work, a line). Everything else about the heartbeat — including the six
# duplicate scenarios below — is pinned at the model level in
# `test_progress_heartbeat.py`; these two are the display-only claims that
# have no model-level twin.


def _heartbeat_line(rig: _Rig, strip_ansi) -> str:
    """The heartbeat row as the last frame drew it.

    Found by the event count rather than by the glyph, which is the thing
    under test in half of these — a helper keyed on a particular frame would
    quietly stop finding the row the moment the beat moved.
    """
    return _line(strip_ansi(rig.output()), " event")


@pytest.fixture
def live(
    as_terminal: None,
    store: RecordStore,
    make_renderer: Callable[..., RichProgressRenderer],
) -> Iterator[_Rig]:
    """A renderer drawing to a terminal, driven a refresh at a time."""
    stream = io.StringIO()
    renderer = make_renderer(stream=stream)
    handler = LumberjackHandler(on_record=renderer.render, level=logging.DEBUG)
    logger = logging.getLogger("heartbeat-rig")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield _Rig(logger, handler, store, renderer, stream)
    finally:
        logger.removeHandler(handler)


def test_the_heartbeat_keeps_no_clock(live: _Rig, make_row, strip_ansi):
    """An elapsed counter would tick through the silence above, which is the
    same lie in a different column."""
    live.store.append([make_row() for _ in range(4)])
    live.renderer.refresh()
    assert not re.search(r"\d+:\d\d:\d\d", _heartbeat_line(live, strip_ansi))


def test_the_heartbeat_is_drawn_above_every_bar(live: _Rig, make_row, strip_ansi):
    """The overflow ellipsis crops from the bottom, and liveness is the row
    worth keeping."""
    live.store.append([make_row(message=f"fetched row {i}") for i in range(5)])
    live.renderer.refresh()
    live.stream.seek(0)
    live.stream.truncate(0)
    live.renderer.refresh()

    frame = strip_ansi(live.output())
    assert "iterations" in frame, "no source bar was drawn to sit under"
    assert frame.index("events") < frame.index("iterations")


# --- what the terminal can actually encode ---------------------------------


@pytest.mark.parametrize("legacy", [True, False])
def test_a_console_draws_only_glyphs_its_encoding_can_take(
    store: RecordStore, make_row, make_renderer, strip_ansi, legacy
):
    """rich swaps its *own* bars for ASCII on a legacy console, and ours have
    to follow, or a row mixes rich's `-` with our `▪` and gets the worst of
    both. The observable contract rather than the mechanism: a collapsed
    row's line is wholly ASCII on a legacy console and free to use the
    richer mark on a modern one.

    Regression test with a history: an earlier fix routed "degrade" through a
    `None` encoding, then `None` was given the opposite meaning — rich's own
    convention, "the stream did not say, assume utf-8" — and this branch went
    back to drawing braille on the one console that cannot take it. Scoped to
    the collapsed row's own line rather than the whole frame, so this is not
    thrown off by other rows using punctuation of their own.
    """
    real_console = rich_renderer_module.Console
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        rich_renderer_module,
        "Console",
        lambda **kwargs: real_console(
            force_terminal=True, width=90, legacy_windows=legacy, **kwargs
        ),
    )
    try:
        stream = io.StringIO()
        renderer = make_renderer(stream=stream)
        store.append([make_row(created=100.0 + i) for i in range(5)])
        renderer.refresh()
        line = _line(strip_ansi(stream.getvalue()), "iterations")
        assert "5 iterations" in line, "the bar did not draw"
        assert line.isascii() == legacy
    finally:
        monkeypatch.undo()


def test_a_stream_that_cannot_encode_the_mark_gets_the_ascii_one(
    store: RecordStore, make_row, make_renderer, strip_ansi
):
    """The second signal, and it fires where the first does not. A modern
    terminal redirected to a stream declaring `ascii` — or a Windows console on
    `cp1252` — is not `legacy_windows`, so rich draws its own box characters
    happily and only lumberjack's glyphs would be unencodable if drawn as-is.
    A write rich cannot encode raises rather than degrading, which would take
    down the `logger.debug()` that reached it, so the collapsed row's own
    line is ASCII too."""

    class _AsciiStream(io.StringIO):
        encoding = "ascii"

    real_console = rich_renderer_module.Console
    monkeypatch = pytest.MonkeyPatch()
    # legacy_windows pinned off so this exercises the encoding signal alone:
    # rich detects it per *platform*, not per stream, so a Windows runner would
    # otherwise take the branch above and never reach this one.
    monkeypatch.setattr(
        rich_renderer_module,
        "Console",
        lambda **kwargs: real_console(
            force_terminal=True, legacy_windows=False, **kwargs
        ),
    )
    try:
        stream = _AsciiStream()
        renderer = make_renderer(stream=stream)
        store.append([make_row(created=100.0 + i) for i in range(5)])
        renderer.refresh()
        line = _line(strip_ansi(stream.getvalue()), "iterations")
        assert "5 iterations" in line, "the bar did not draw"
        assert line.isascii()
    finally:
        monkeypatch.undo()
