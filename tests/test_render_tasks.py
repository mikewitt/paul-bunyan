"""Named task bars on screen: rung 2 of the value ladder, drawn.

Every number here came from a `task()` or `track()` call that stated it — no
inference. `task_rig` publishes a session so the tracking API's records reach
the handler by way of the *root* logger, which is where `init()` would have
put it, and drives the same real path as `test_render_progress.py`'s `rig`:
stdlib `logging` → `LumberjackHandler` → `RecordStore` → bar.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import re
from collections.abc import Iterator

import pytest

pytest.importorskip("rich")

import lumberjack  # noqa: E402
import lumberjack.renderers.rich_renderer as rich_renderer_module  # noqa: E402
import lumberjack.tracking  # noqa: E402
from lumberjack import session  # noqa: E402
from lumberjack.detect import OutputMode  # noqa: E402
from lumberjack.handler import LumberjackHandler  # noqa: E402
from lumberjack.renderers.rich_renderer import RichProgressRenderer  # noqa: E402
from lumberjack.session import Session  # noqa: E402
from lumberjack.store import RecordStore  # noqa: E402


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
def as_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the renderer's console believe it is talking to a real terminal."""
    real_console = rich_renderer_module.Console
    monkeypatch.setattr(
        rich_renderer_module,
        "Console",
        # legacy_windows pinned off: on a Windows runner rich detects it and
        # swaps its own `━` for `-`, so an assertion about a bar's shape would
        # fail there for a reason that has nothing to do with the display.
        lambda **kwargs: real_console(
            force_terminal=True, width=100, legacy_windows=False, **kwargs
        ),
    )


def _line(frame: str, needle: str) -> str:
    """The needle's line as the *last* frame drew it.

    A live display rewrites in place, so the captured stream holds every frame
    since the first, separated by carriage returns as well as newlines. The
    interesting one is always the most recent.
    """
    return next(ln for ln in reversed(re.split(r"[\r\n]", frame)) if needle in ln)


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


def test_a_task_draws_a_named_determinate_bar(task_rig, strip_ansi):
    """Rung 2 on screen: the label the code chose, and a real percentage,
    because `task()` said how much work there was."""
    with lumberjack.task("reindex", total=100) as t:
        t.set_progress(40)
    task_rig.tick()
    out = strip_ansi(task_rig.output())
    assert "reindex" in out
    assert "40/100" in out
    assert "40%" in out


def test_a_task_without_a_total_pulses_instead_of_claiming_one(task_rig, strip_ansi):
    """`total=None` is rich's indeterminate bar. Inventing a denominator
    would be the one thing a bar must never do.

    Drawn *while the task is open*: once it ends, filling the bar is correct
    and is what the test below pins.
    """
    with lumberjack.task("scan") as t:
        t.advance()
        task_rig.tick()
        out = strip_ansi(task_rig.output())
    assert "scan" in out
    assert "%" not in out, "an indeterminate task must not show a percentage"


def test_a_task_that_ends_without_a_total_still_finishes(task_rig, strip_ansi):
    """An `end` row is an exact completion signal — the only kind of bar here
    that has one. Leaving it pulsing would say "still working" about work that
    is provably over."""
    with lumberjack.task("scan") as t:
        t.advance()
    task_rig.tick()
    line = _line(strip_ansi(task_rig.output()), "scan")
    assert "100%" in line, "an ended task was left pulsing"


def test_a_container_task_shows_no_count_at_all(task_rig, strip_ansi):
    """A bare 0 beside a pulsing bar reads as "stuck at zero" rather than
    "no count was claimed"."""
    handle = lumberjack.task("etl run")
    try:
        task_rig.tick()
        line = next(
            ln for ln in strip_ansi(task_rig.output()).splitlines() if "etl run" in ln
        )
        # Everything but the elapsed clock, which is always digits.
        assert not re.search(r"\d", re.sub(r"\d+:\d\d:\d\d", "", line))
    finally:
        handle.end()


def test_subtasks_are_indented_under_their_parent(task_rig, strip_ansi):
    with lumberjack.task("outer") as outer:
        with outer.subtask("inner"):
            task_rig.tick()
    out = strip_ansi(task_rig.output())
    outer_line = next(ln for ln in out.splitlines() if "outer" in ln)
    inner_line = next(ln for ln in out.splitlines() if "inner" in ln)
    assert len(inner_line) - len(inner_line.lstrip()) > len(outer_line) - len(
        outer_line.lstrip()
    )


def test_a_task_draws_no_duplicate_source_bar(task_rig, strip_ansi):
    """Task events are ordinary records located at the `task()` call line, so
    without the store-side filter every named bar would get a pulsing
    source-location bar drawn beside it."""
    with lumberjack.task("reindex", total=10) as t:
        for _ in range(5):
            t.advance()
    task_rig.tick()
    assert "reindex" in strip_ansi(task_rig.output())
    # The source model is the observable contract here: a substring check on
    # the frame is defeated by column truncation at narrow widths.
    assert task_rig.renderer.bars() == [], "the task's own call site got a bar"


def test_named_bars_are_drawn_above_source_bars(task_rig, strip_ansi):
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
    frame = strip_ansi(task_rig.output())
    assert "zzexact" in frame and "iterations" in frame
    assert frame.index("zzexact") < frame.index("iterations"), "source bars drew first"


def test_a_task_drawn_determinate_then_overshooting_withdraws_its_claim(
    task_rig, strip_ansi, monkeypatch
):
    """A bar created *already* overshot is indeterminate from birth, which
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
        assert "50%" in _line(strip_ansi(task_rig.output()), "underestimated")
        t.set_progress(25)
        task_rig.tick()
        line = _line(strip_ansi(task_rig.output()), "underestimated")
    assert "25/10" in line, "the real numbers must stay visible"
    assert "%" not in line, "the withdrawn claim came back as a full bar"


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


def test_a_container_task_finishes_at_a_hundred_percent(task_rig, strip_ansi):
    """A task that only ever held subtasks has no count of its own, so its
    total is 0 — and rich renders 0-of-0 as a full bar labelled 0%, which
    reads as a failure rather than as completion."""
    with lumberjack.task("etl run"):
        pass
    task_rig.tick()
    line = _line(strip_ansi(task_rig.output()), "etl run")
    assert "100%" in line
    assert " 0%" not in line, "0 of 0 rendered as a full bar labelled 0%"


def test_a_task_that_ends_short_of_its_total_keeps_both_numbers(task_rig, strip_ansi):
    """Stopping at 5 of a claimed 10 is a fact about the run. Filling the bar
    would overwrite it with a claim of 10, which is the opposite of what
    finishing a bar is supposed to communicate."""
    with lumberjack.task("gave up early", total=10) as t:
        t.set_progress(5)
    task_rig.tick()
    line = _line(strip_ansi(task_rig.output()), "gave up early")
    assert "50%" in line and "5/10" in line
