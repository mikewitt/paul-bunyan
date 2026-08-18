"""rich holds exactly what the frame planned, or this fails.

`plan_frame()` decides and `RichProgressRenderer._sync()` paints. That split
is only worth having if the two are held together, because the failure it
exists to prevent is precisely a decider and a painter that agree with
themselves and disagree with each other. Everything here is that one
assertion, run over one scenario per branch of the draw path.

**Why the full tuple and not a count and a description.** Measured on the
commit before the extraction, two mutations passed the entire 606-test suite:

| mutation | full suite | this file |
|---|---|---|
| source rows lose total withdrawal (`_set_total` → `update`) | 606 passed | fails |
| determinate fill uses `row.count`, not `row.cycle_current` | 606 passed | fails |

Neither is theoretical. A source row that cannot withdraw a total keeps
`completed > total`, which rich clamps to a **full green bar for a loop that
is still running** — the one thing the pulse rule exists to prevent. And no
frame-text assertion can ever catch it: rendered into a console,
`(completed=10, total=None)` and `(completed=10, total=5)` are byte-identical
after `strip_ansi`. The test written to catch it
(`test_a_retired_source_bar_that_resumes_stops_claiming_completion`) asserted
`"%" not in line`, which is vacuously true — `_source_progress` carries no
`TaskProgressColumn`, so no source row has ever contained a percent sign.

**`counts()` is in the assertion for a reason.** It is computed from rich's
own task dicts, so `renderer.counts() == frame.counts` compares the screen
against the plan. The day it becomes `return self._frame.counts` this file
starts comparing the plan against itself and proving nothing.

**One scenario per branch.** A new branch in `_loop_row`, `_position_row` or
`_task_row` earns a scenario in the same commit. This is not belt and braces:
the `_relayout` no-op mutation is invisible on any single-row scenario and
only diverges once a nested, two-worker case exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

pytest.importorskip("rich")

from fixture_sources import SEQUENCE, STAGES
from lumberjack.renderers.plan import Frame
from lumberjack.renderers.rich_compat import _COLLAPSED, _SUBROW
from lumberjack.renderers.rich_renderer import RichProgressRenderer
from lumberjack.store import RecordStore


@pytest.fixture
def make_renderer(store: RecordStore) -> Iterator[Callable[..., RichProgressRenderer]]:
    created: list[RichProgressRenderer] = []

    def _make(**kwargs: object) -> RichProgressRenderer:
        import io

        renderer = RichProgressRenderer(
            store,
            stream=io.StringIO(),
            min_repeats=3,
            refresh_interval=0,
            **kwargs,  # type: ignore[arg-type]
        )
        created.append(renderer)
        return renderer

    yield _make
    for renderer in created:
        renderer.close()


def _rich_rows(renderer: RichProgressRenderer) -> list[tuple[object, ...]]:
    """Every field rich holds per source task, in render order."""
    return [
        (
            task.description,
            task.total,
            int(task.completed),
            task.fields.get("rate"),
            task.fields.get("detail"),
            bool(task.fields.get(_COLLAPSED)),
            bool(task.fields.get(_SUBROW)),
        )
        for task in renderer._source_progress.tasks
    ]


def _planned_rows(frame: Frame) -> list[tuple[object, ...]]:
    return [
        (
            row.label,
            row.total,
            row.completed,
            row.rate,
            row.detail,
            row.collapsed,
            row.subrow,
        )
        for row in frame.rows
    ]


def _rich_tasks(renderer: RichProgressRenderer) -> list[tuple[object, ...]]:
    return [
        (task.description, task.total, int(task.completed), task.fields.get("count"))
        for task in renderer._task_progress.tasks
    ]


def _planned_tasks(frame: Frame) -> list[tuple[object, ...]]:
    return [(row.label, row.total, row.completed, row.count) for row in frame.tasks]


# --- the scenarios ----------------------------------------------------------
#
# Each yields once per poll it wants checked, so the parity assertion runs at
# every step rather than only at the end: a row that is right when the dust
# settles and wrong on the way there is still wrong on screen.


def pulsing_loop(store, make_row, make_task_row):
    """The permanent state of an outermost loop: nothing bounds it."""
    for cycle in range(4):
        store.append([make_row(msg="parsed %d", created=100.0 + cycle)])
        yield f"cycle {cycle}"


def determinate_nested_loop(store, make_row, make_task_row):
    """Containment settles, so the inner row gets a total and fills against
    the *cycle* rather than the run. The mutation that swapped those two
    passed the whole suite."""
    at = 100.0
    for outer in range(6):
        store.append([make_row(lineno=8, msg="reconciling batch %d", created=at)])
        store.append(
            [
                make_row(lineno=10, msg="compared row %d", created=at + 0.01 * i)
                for i in range(1, 21)
            ]
        )
        at += 1.0
        yield f"outer {outer}"


def idle_then_resumed(store, make_row, make_task_row):
    """The withdrawal, in both directions. Retiring fills the row and gives it
    a total; resuming has to shed that total or the bar reads finished while
    the work runs on."""
    import time

    store.append([make_row(msg="working %d", created=100.0 + i) for i in range(5)])
    yield "first burst"
    yield "gone quiet"
    now = time.time()
    store.append(
        [make_row(msg="working %d", created=now - 0.4 + i * 0.1) for i in range(5)]
    )
    yield "resumed"


def slow_loop_with_position(store, make_row, make_task_row, path):
    """A body slow enough to earn the second row, then quiet enough to
    collapse it along with the loop above."""
    at = 100.0
    for cycle in range(4):
        for index, template in enumerate(STAGES):
            store.append(
                [
                    make_row(
                        pathname=path,
                        lineno=8 + index,
                        func_name="run",
                        msg=template,
                        created=at + index,
                    )
                ]
            )
        at += 3.0
        yield f"cycle {cycle}"


def task_bar_shapes(store, make_row, make_task_row):
    """The four branches of `_task_row`, one per poll."""
    store.append([make_task_row(1, "start", label="no total", created=100.0)])
    yield "no total"
    claimed = {"label": "claimed", "progress_total": 10}
    store.append([make_task_row(2, "start", created=101.0, **claimed)])
    store.append(
        [make_task_row(2, "update", progress_current=25, created=102.0, **claimed)]
    )
    yield "overshoot withdraws"
    store.append(
        [make_task_row(2, "end", progress_current=5, created=103.0, **claimed)]
    )
    yield "ended short keeps both"
    store.append([make_task_row(1, "end", label="no total", created=104.0)])
    yield "container ends"


SCENARIOS = [
    pulsing_loop,
    determinate_nested_loop,
    idle_then_resumed,
    task_bar_shapes,
]


def _assert_parity(renderer: RichProgressRenderer, step: str) -> None:
    frame = renderer.frame
    assert frame is not None
    assert _rich_rows(renderer) == _planned_rows(frame), step
    assert _rich_tasks(renderer) == _planned_tasks(frame), step
    assert renderer.counts() == frame.counts, step


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
def test_rich_holds_exactly_what_the_frame_planned(
    scenario, store, make_row, make_task_row, make_renderer
):
    renderer = make_renderer()
    drew = False
    for step in scenario(store, make_row, make_task_row):
        renderer.refresh()
        _assert_parity(renderer, step)
        frame = renderer.frame
        assert frame is not None
        drew = drew or bool(frame.rows) or bool(frame.tasks)
    assert drew, "a parity assertion over an empty frame proves nothing"


def test_a_position_row_stays_in_step_with_the_loop_above_it(
    store, make_row, make_task_row, make_renderer, write_module
):
    """Its own test because it needs a real module on disk: the ordinal comes
    from the AST, so a fabricated path yields no second row at all."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
    renderer = make_renderer()
    saw_position = False
    for step in slow_loop_with_position(store, make_row, make_task_row, path):
        renderer.refresh()
        _assert_parity(renderer, step)
        frame = renderer.frame
        assert frame is not None
        saw_position = saw_position or frame.counts.positions > 0
    assert saw_position, "the scenario never admitted the row it exists to check"


# --- where the two deliberately disagree ------------------------------------


def test_the_ceiling_leaves_a_row_on_screen_the_frame_no_longer_plans(
    store, make_row, make_renderer
):
    """The one place `counts()` and `frame.counts` diverge, pinned as it is.

    `_relayout` keeps tasks absent from the order it is given (rich_compat),
    and `_display_order` re-sorts by liveness every poll — so ceiling
    membership churns, and a row truncated out *after* being registered keeps
    its rich `Task`, frozen at whatever it last said and parked at the end of
    the display. The frame stops planning it; the screen keeps showing it.

    Asserted rather than fixed. The fix is a behaviour change to the shipped
    display — either `_relayout` gains authority to set `visible=False`, or a
    truncated row is redrawn collapsed before it is dropped — and that belongs
    in its own commit with its own argument. What this change does is make the
    disagreement a *number* instead of an unnoticed bar.

    It is also the only scenario in which `counts()` reading rich rather than
    the frame is observable, so it is what stops that method being
    "simplified" into `return self._frame.counts`.
    """
    clock = [100.5]
    renderer = make_renderer(max_bars=2, clock=lambda: clock[0])
    for offset, thread, template in (
        (0, 1, "alpha %d"),
        (1, 2, "bravo %d"),
        (2, 3, "charlie %d"),
    ):
        store.append(
            [
                make_row(
                    lineno=10 + offset,
                    thread=thread,
                    msg=template,
                    created=100.0 + i * 0.1,
                )
                for i in range(5)
            ]
        )
    renderer.refresh()
    _assert_parity(renderer, "everything still live")

    # alpha goes quiet; the other two keep going. A live subtree sorts above a
    # collapsed one, so alpha falls past the ceiling it was inside.
    clock[0] = 101.6
    for offset, thread, template in ((1, 2, "bravo %d"), (2, 3, "charlie %d")):
        store.append(
            [
                make_row(
                    lineno=10 + offset,
                    thread=thread,
                    msg=template,
                    created=101.0 + i * 0.1,
                )
                for i in range(5)
            ]
        )
    renderer.refresh()

    frame = renderer.frame
    assert frame is not None
    assert [row.label for row in frame.rows] == ["bravo …", "charlie …"]
    assert frame.counts.drawn_loops == 2
    # ...and rich still holds three, alpha among them.
    assert renderer.counts().drawn_loops == 3
    assert [task.description for task in renderer._source_progress.tasks][
        -1
    ] == "alpha …"
