"""Tests for the named bars the tracking API reports.

No inference here at all: every number came from a `task()` or `track()`
call that stated it. This model only reads.

No `rich` here either — the model is display-independent, so it runs on a
bare install.
"""

from __future__ import annotations

from lumberjack.renderers.progress import TaskProgressModel


def test_a_task_gets_a_bar_on_its_start_row(store, make_task_row):
    store.append([make_task_row(1, "start", label="reindex")])
    (bar,) = TaskProgressModel(store).poll()
    assert (bar.task_id, bar.label, bar.done) == (1, "reindex", False)


def test_the_latest_row_wins_rather_than_accumulating(store, make_task_row):
    """`progress_current` is absolute, so the newest row for a task is the
    whole truth about it — which is what makes a named bar exact."""
    model = TaskProgressModel(store)
    store.append(
        [
            make_task_row(1, "start", progress_current=0, progress_total=100),
            make_task_row(1, "update", progress_current=40, progress_total=100),
        ]
    )
    (bar,) = model.poll()
    assert (bar.current, bar.total) == (40, 100)


def test_polling_twice_without_new_rows_changes_nothing(store, make_task_row):
    model = TaskProgressModel(store)
    store.append([make_task_row(1, "update", progress_current=7)])
    assert [b.current for b in model.poll()] == [7]
    assert [b.current for b in model.poll()] == [7]


def test_a_task_without_a_total_is_indeterminate(store, make_task_row):
    store.append([make_task_row(1, "update", progress_current=7)])
    (bar,) = TaskProgressModel(store).poll()
    assert bar.total is None


def test_a_task_with_a_total_is_determinate(store, make_task_row):
    store.append([make_task_row(1, "update", progress_current=7, progress_total=10)])
    (bar,) = TaskProgressModel(store).poll()
    assert bar.total is not None


def test_the_end_row_finishes_the_bar(store, make_task_row):
    """An exact completion signal, unlike a source bar, which has none."""
    model = TaskProgressModel(store)
    store.append([make_task_row(1, "start")])
    assert [b.done for b in model.poll()] == [False]
    store.append([make_task_row(1, "end", progress_current=9)])
    (bar,) = model.poll()
    assert bar.done and bar.current == 9


def test_depth_comes_from_the_parent_chain(store, make_task_row):
    store.append(
        [
            make_task_row(1, "start", label="root"),
            make_task_row(2, "start", label="child", parent_task_id=1),
            make_task_row(3, "start", label="grandchild", parent_task_id=2),
        ]
    )
    assert [(b.label, b.depth) for b in TaskProgressModel(store).poll()] == [
        ("root", 0),
        ("child", 1),
        ("grandchild", 2),
    ]


def test_a_broken_parent_chain_does_not_hang(store, make_task_row):
    """Defensive only against data, not against a bug: a store trimmed by
    `evict()` can leave a child whose parent's rows are gone."""
    store.append([make_task_row(2, "start", label="orphan", parent_task_id=99)])
    (bar,) = TaskProgressModel(store).poll()
    assert bar.depth == 0


def test_task_bars_keep_their_slot(store, make_task_row):
    """Append-only, as source bars are: a bar that moves is unreadable.
    Children land after parents for free, since a parent must exist before
    `subtask()` can be called on it."""
    model = TaskProgressModel(store)
    store.append([make_task_row(1, "start", label="first")])
    model.poll()
    store.append(
        [
            make_task_row(2, "start", label="second"),
            make_task_row(1, "update", label="first", progress_current=99),
        ]
    )
    assert [b.label for b in model.poll()] == ["first", "second"]
