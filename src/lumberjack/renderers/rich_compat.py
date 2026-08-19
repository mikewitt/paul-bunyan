"""Reaching into rich where there is no public API to ask for it.

`rich.progress.Progress` has no supported way to do three things
`RichProgressRenderer` needs: withdraw a task's total back to indeterminate
once it has been given one (`_set_total`), reorder tasks in place without
recreating them and losing their elapsed clock (`_relayout`), or draw a bar
that is sometimes a bar and sometimes a mark depending on state the built-in
`ProgressColumn`s have no field for (`_RowBarColumn` and its siblings below).
**Code belongs in this module only if it reaches into rich's private state
(`Progress._lock`, `Progress._tasks`) or subclasses a rich internal to work
around a missing public one** — everything else about the progress display,
however rich-specific, stays in `rich_renderer.py`.

Guarded the same way `rich_renderer.py` is, and independently: this module
does its own `try`/`except ImportError` around its `rich` imports rather than
depending on `rich_renderer.py` having already done so, so it carries no
import-order relationship to that module. It is still only ever imported
*from* `rich_renderer.py`, itself reachable only where `rich` is already
known to be installed (see that module's docstring for the shape of the
guard), so this file is never reached — and `rich` is never imported — on a
bare install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

try:
    from rich.progress import (
        BarColumn,
        Progress,
        ProgressColumn,
        TaskID,
        TimeElapsedColumn,
    )
    from rich.table import Column
    from rich.text import Text
except ImportError:  # pragma: no cover - exercised only without rich installed
    Column = None  # type: ignore[assignment,misc]
    Progress = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    BarColumn = None  # type: ignore[assignment,misc]
    # Subclassed at module scope below, so unlike the rest it needs to stay a
    # class even where rich is absent. The subclass is never instantiated on
    # that path — `RichProgressRenderer.__init__` raises first.
    ProgressColumn = object  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.progress import Task

    from lumberjack.renderers.plan import PlanRow


def _set_total(progress: Progress, task_id: TaskID, total: int | None) -> None:
    """Set a rich task's total, including *back* to None.

    `Progress.update(total=None)` does not withdraw a total — rich documents
    it as "updates task.total if not None", so None reads as "not supplied"
    and the old total survives. `Progress.reset()` says the same thing in the
    same words. There is no public way to make a determinate task
    indeterminate again, so this reaches for `_tasks` under the progress's own
    lock, which is exactly what `reset()` does.

    Without it every path back to a pulse is silently a no-op, and rich clamps
    the stale `completed > total` to a full bar — a bar reading "finished"
    while the work runs on, which is the one thing the pulse rule exists to
    prevent.

    `finished_time` is cleared with it. rich latches that the moment
    `completed >= total` and `Task.elapsed` returns it forever after, so a bar
    that touched 100% before the claim was withdrawn would keep a frozen
    clock. Only on an actual change, so a legitimately finished bar keeps its
    stopped timer.
    """
    with progress._lock:  # noqa: SLF001 - no public withdrawal exists; see above
        task = progress._tasks[task_id]  # noqa: SLF001
        if task.total == total:
            return
        task.total = total
        task.finished_time = None


def _relayout(progress: Progress, order: list[TaskID]) -> None:
    """Re-lay-out a `Progress`'s rows without disturbing the rows themselves.

    `Progress.tasks` is `list(self._tasks.values())` and `_tasks` is a plain
    dict, so **insertion order is render order** and placement is otherwise
    decided once, at `add_task()`. Rebuilding that dict in a new order is the
    whole mechanism (verified against rich 15.0.0, and pinned by a test so an
    upgrade that changes it fails loudly rather than silently scrambling the
    display).

    **Reordered, never removed and re-added.** `remove_task()` plus a fresh
    `add_task()` would put the row in the right place and throw away the
    `Task` — its start time, its elapsed clock, its completion — so every
    structural change would reset the timers of the rows it moved.

    Private access for the same reason `_set_total` has it: rich exposes no
    public way to order tasks. Anything already in the dict but absent from
    `order` is kept, at the end, so a caller that has stopped drawing a row
    cannot silently delete it here.
    """
    with progress._lock:  # noqa: SLF001 - no public reorder exists; see above
        current = progress._tasks  # noqa: SLF001
        wanted = [task_id for task_id in order if task_id in current]
        if len(wanted) < len(current):
            placed = set(wanted)
            wanted += [task_id for task_id in current if task_id not in placed]
        if wanted == list(current):
            return
        reordered = {task_id: current[task_id] for task_id in wanted}
        progress._tasks = reordered  # noqa: SLF001


#: The task field that says a row has collapsed. Carried on the rich `Task`
#: rather than looked up per render, because a column is handed a `Task` and
#: nothing else.
_COLLAPSED = "collapsed"

#: The task field marking a position row — the second row a slow loop earns,
#: drawn under the loop it belongs to. Set for the same reason `_COLLAPSED`
#: is: the trailing columns have to know, and a column sees only a `Task`.
_SUBROW = "subrow"


def _plan_fields(row: PlanRow) -> dict[str, Any]:
    """The custom cells every progress task carries, taken off a planned row.

    Spread with `**` at the call site rather than handed over as `fields=`.
    `Progress.add_task` collects `**fields`, so `fields={...}` stores one entry
    literally named "fields" and none of the real keys exist — a column reading
    `task.fields["subrow"]` then sees nothing until the first `update()`
    happens to set it. Harmless where an `update()` follows immediately and a
    trap everywhere else, so the bundle is built in one place and splatted.

    One mapping for both populations: a task bar carries `count` and blank
    loop cells, a loop row carries `rate` and `detail` and a blank count. They
    share a shape so the mapper has one code path, and so a column added to
    one is a column the other explicitly blanks rather than silently omits —
    an omitted key reads as `None` in a column and renders as "None".
    """
    return {
        "rate": row.rate,
        "detail": row.detail,
        "count": row.count,
        _COLLAPSED: row.collapsed,
        _SUBROW: row.subrow,
    }


#: The mark a collapsed row shows where a running one shows a bar, and its
#: fallback for an output encoding that cannot carry it. `cp1252` — a Windows
#: console's default — encodes neither this nor the heartbeat's braille, and an
#: unencodable write raises rather than degrading, which would take down the
#: `logger.debug()` that reached it. rich substitutes its *own* box and bar
#: characters on a limited encoding and cannot know to do the same for a
#: character lumberjack chose.
#:
#: Kept beside `_RowBarColumn` rather than in `rich_renderer.py`: `_COLLAPSED_BAR`
#: is that column's default `collapsed_mark`, so the two live together the way
#: `_plan_fields`'s field constants do. `rich_renderer.py` imports both back to
#: pick between them by encoding — see its `__init__`.
_COLLAPSED_BAR = "▪"
_COLLAPSED_BAR_ASCII = "#"


class _RowBarColumn(ProgressColumn):
    """A bar while the loop runs; a mark once it has gone quiet.

    **Collapsing cannot save vertical space and is not trying to.** A retired
    bar is marked idle *in place* — deleting the row would empty the final
    frame that "drain before closing" exists to preserve, and would say the
    work stopped existing rather than stopped. So what collapses is the row's
    width and its weight: forty columns of finished bar, restated on every
    redraw for work that ended minutes ago, is the loudest element on screen
    saying the least.

    A grid column is as wide as its widest cell, so this also shrinks the whole
    bar column to one character once *every* row has gone quiet — which is
    exactly the frame a finished run leaves behind.

    Delegates to a real `BarColumn` rather than subclassing it, because
    `BarColumn.render` is annotated as returning a `ProgressBar` and this
    returns text half the time.
    """

    def __init__(self, collapsed_mark: str = _COLLAPSED_BAR) -> None:
        self._bar = BarColumn()
        self._collapsed_mark = collapsed_mark
        super().__init__()

    @override
    def render(self, task: Task) -> RenderableType:
        if task.fields.get(_COLLAPSED):
            return Text(self._collapsed_mark, style="bar.finished")
        return self._bar.render(task)


class _RowTextColumn(ProgressColumn):
    """One text cell of a loop row, dimmed once the row has collapsed.

    `TextColumn` does everything but that: its style is fixed at construction,
    where this one has to depend on the task.
    """

    def __init__(
        self, field: str | None = None, *, style: str = "progress.description"
    ) -> None:
        self._field = field
        self._style = style
        # As `TextColumn` does, and for the same reason: a label is a message
        # template and a wrapped one would push every row below it down the
        # screen.
        super().__init__(table_column=Column(no_wrap=True))

    @override
    def render(self, task: Task) -> Text:
        value = (
            task.description
            if self._field is None
            else str(task.fields.get(self._field, ""))
        )
        # Never markup: labels carry file paths and message templates, and a
        # stray "[" in either must not parse as a rich tag.
        return Text(value, style="dim" if task.fields.get(_COLLAPSED) else self._style)


class _RowElapsedColumn(ProgressColumn):
    """Elapsed time, except on a position row, which has none to report.

    A loop row's clock measures how long the loop has been running, which is a
    fact about the work. A position row's would measure how long ago the
    display started drawing it — near-identical to the loop's, restated one
    line below it, and describing nothing anybody asked about. Blank is the
    honest cell: the subordinate row carries the stage and its ordinal, and
    borrows every other number from the row above.
    """

    def __init__(self) -> None:
        self._elapsed = TimeElapsedColumn()
        super().__init__()

    @override
    def render(self, task: Task) -> RenderableType:
        if task.fields.get(_SUBROW):
            return Text("")
        return self._elapsed.render(task)
