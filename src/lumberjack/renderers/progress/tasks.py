"""Named bars, from what the tracking API actually reported.

The exact half. Every number here came from a `task()` or `track()` call that
stated it outright, so nothing in this module guesses — which is why it shares
nothing with the inference in `sources.py` but the `RecordStore` interface.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lumberjack.store import RecordStore


@dataclasses.dataclass(frozen=True, slots=True)
class TaskBarState:
    """One named bar, built from what the tracking API actually reported.

    Unlike `BarState` this has a `total` — sometimes. `task()` without one is
    an indeterminate task, and the display says so by pulsing rather than by
    inventing a denominator.
    """

    task_id: int
    label: str
    current: int
    total: int | None
    done: bool
    depth: int

    @property
    def is_determinate(self) -> bool:
        return self.total is not None


class TaskProgressModel:
    """Folds `store.task_events_since()` into one bar per task.

    A task bar is the *latest state* of a task rather than a tally, which is
    what makes it exact: `progress_current` is absolute, so the newest row for
    a task is the whole truth about it and no accumulation is needed. That
    also means a bar cannot drift — there is nothing to drift from.

    No inference of any kind lives here. Every number came from a `task()` or
    `track()` call that said so outright. Phase 4b's model is the one that
    guesses; this one only reads.
    """

    def __init__(self, store: RecordStore) -> None:
        self._store = store
        # Unbounded: a task that ended keeps its bar, because one that vanished
        # mid-run would read as "this work stopped existing". A long-running
        # process opening many short tasks therefore accumulates rows. Source
        # bars answered their version of this by merging call sites into loops,
        # which does not carry over — two `task()` calls are two things the
        # author named separately, and folding them together would mean
        # matching labels as text. The answer here is the screen budget
        # instead. lumberjack: see issue #68
        self._states: dict[int, TaskBarState] = {}
        # Append-only display order, as for source bars: a bar that moves is
        # unreadable. Children land after parents for free, because a parent
        # must exist before `subtask()` can be called on it.
        self._order: list[int] = []
        self._parents: dict[int, int | None] = {}
        self._watermark = 0

    def poll(self) -> list[TaskBarState]:
        """Fold in whatever arrived since the last call and return the bars."""
        delta = self._store.task_events_since(self._watermark)
        self._watermark = delta.last_id
        for event in delta.events:
            if event.task_id not in self._states:
                self._order.append(event.task_id)
                self._parents[event.task_id] = event.parent_task_id
            self._states[event.task_id] = TaskBarState(
                task_id=event.task_id,
                label=event.label,
                # A `start` row carries current=0; an `end` row carries the
                # final count unsampled. Either way the newest row wins.
                current=event.current or 0,
                total=event.total,
                done=event.event == "end",
                depth=self._depth(event.task_id),
            )
        return self.bars()

    def _depth(self, task_id: int) -> int:
        """How deep this task sits in the parent chain.

        Walked rather than stored because a child can be created before its
        parent's first row is folded in — `subtask()` emits `start` for the
        child, and the parent's own rows may already be behind the watermark.
        The chain is shallow and the walk is a dict lookup per level.
        """
        depth = 0
        seen = {task_id}
        parent = self._parents.get(task_id)
        # Only ancestors we can actually draw count. `evict()` can trim a
        # parent's rows while a child's survive, and indenting that child
        # under nothing reads as a rendering bug rather than as hierarchy.
        # The `seen` guard is for a cycle, which the tracking API cannot
        # produce but a hand-written store row could.
        while parent is not None and parent in self._states and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = self._parents.get(parent)
        return depth

    def bars(self) -> list[TaskBarState]:
        """The most recent poll's bars, in display order."""
        return [self._states[task_id] for task_id in self._order]
