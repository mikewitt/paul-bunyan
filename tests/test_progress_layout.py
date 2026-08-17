"""Row order: structure decides it, arrival order breaks the ties.

No `rich` here — the ordering is a property of what was inferred, not of how
it is drawn, so it runs on a bare install like the rest of the models.
"""

from __future__ import annotations

import pytest

from lumberjack.renderers.progress import depth_first_order
from lumberjack.schema import SourceKey


def _src(lineno: int) -> SourceKey:
    return SourceKey("/nonexistent/foo.py", lineno, "bar")


def test_a_child_follows_its_parent_however_late_the_parent_arrived():
    """The defect in one line: an inner loop logs many times per outer
    iteration, so it always qualifies first."""
    child, parent = _src(6), _src(4)
    assert depth_first_order([child, parent], {child: parent}) == [parent, child]


def test_a_child_does_not_follow_an_unrelated_row_that_happens_to_precede_it():
    intruder, parent, child = _src(9), _src(4), _src(6)
    assert depth_first_order([intruder, child, parent], {child: parent}) == [
        intruder,
        parent,
        child,
    ]


def test_siblings_under_one_parent_keep_arrival_order():
    parent, first, second = _src(4), _src(6), _src(7)
    assert depth_first_order(
        [second, first, parent], {first: parent, second: parent}
    ) == [parent, second, first]


def test_three_levels_come_out_outermost_first():
    outer, middle, inner = _src(4), _src(6), _src(8)
    assert depth_first_order(
        [inner, middle, outer], {inner: middle, middle: outer}
    ) == [outer, middle, inner]


def test_a_parent_that_is_not_drawn_leaves_the_child_at_the_top_level():
    """The ordinary case for a nested loop whose enclosing line has not yet
    repeated often enough to earn a row of its own."""
    child, absent = _src(6), _src(4)
    assert depth_first_order([child], {child: absent}) == [child]


def test_a_containment_cycle_still_returns_every_row():
    """Pairings are frozen once believed while periods keep moving, so a later
    poll really can name A as B's parent when B is already A's. Two oddly
    placed rows is the cheap wrong answer; two missing rows is not."""
    a, b, c = _src(1), _src(2), _src(3)
    assert sorted(depth_first_order([a, b, c], {a: b, b: a})) == sorted([a, b, c])
    assert depth_first_order([a, b, c], {a: b, b: a})[0] == c


# --- rich's half of the contract ---------------------------------------
#
# `depth_first_order` above decides *what* order a re-layout wants; these pin
# that rich's `Progress` actually renders in that order and survives being
# reordered underneath it. Both are claims about somebody else's library
# rather than about lumberjack, so they are pinned separately and by
# themselves: an upgrade that changes either must fail here rather than
# silently scrambling the display. Each imports `rich` locally and skips
# without it, so the rest of this file still runs on a bare install.


def test_rich_renders_progress_tasks_in_insertion_order():
    """The mechanism the re-layout rests on. `Progress.tasks` is
    `list(self._tasks.values())` over a plain dict, so the dict's order is the
    screen's order and rebuilding it moves rows."""
    pytest.importorskip("rich")
    import lumberjack.renderers.rich_renderer as rich_renderer_module

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
    pytest.importorskip("rich")
    import lumberjack.renderers.rich_renderer as rich_renderer_module
    from lumberjack.renderers.rich_compat import _relayout

    progress = rich_renderer_module.Progress()
    first = progress.add_task("first", total=10)
    second = progress.add_task("second", total=10)
    progress.update(first, completed=7)
    before = next(t for t in progress.tasks if t.id == first)

    _relayout(progress, [second, first])

    after = next(t for t in progress.tasks if t.id == first)
    assert after is before, "the Task was recreated rather than moved"
    assert after.completed == 7
    assert after.start_time == before.start_time
    assert [task.id for task in progress.tasks] == [second, first]


def test_relayout_keeps_rows_the_caller_did_not_mention():
    """A row missing from the order is a caller that stopped drawing it — the
    bar ceiling does exactly that — and losing it here would delete work from
    the screen for a reason that has nothing to do with structure."""
    pytest.importorskip("rich")
    import lumberjack.renderers.rich_renderer as rich_renderer_module
    from lumberjack.renderers.rich_compat import _relayout

    progress = rich_renderer_module.Progress()
    first = progress.add_task("first")
    second = progress.add_task("second")
    _relayout(progress, [second])
    assert [task.id for task in progress.tasks] == [second, first]
