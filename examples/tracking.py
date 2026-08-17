"""Rung 2 of the value ladder: saying explicitly what the work is.

    uv run python examples/tracking.py

`examples/demo.py` is rung 1 — ordinary `logger.debug(...)` calls in code
that has never heard of lumberjack, collapsed into bars by `init()` alone.
This is the other rung: the same pipeline, instrumented with `task()` and
`track()`, so the run *records* named tasks with exact counts and a real
hierarchy instead of counts inferred from how often a line repeated.

Those named tasks are what the top bars are drawn from: a real percentage
where a total was given, a pulse where it was not, indented by task depth,
and finished on the closing record. The inferred source-location bars are
still there underneath — instrumenting some of a program never turns the
rest of it off. The summary below reads the same numbers back out of the
store, which is where the display got them.

Nothing here requires `init()`. Comment it out and the program still runs,
still correct, and silent — which is the point: a *library* can be written
this way without imposing a dependency or any output on the applications
that use it. What the instrumentation becomes is the application's call:

    (nothing installed)      → nothing
    OTel configured          → spans
    lumberjack.init()        → records in the store
    both                     → both

To watch it under OTel instead, install the extra and configure a provider
the ordinary way; lumberjack neither sets one up nor requires one.
"""

from __future__ import annotations

import logging
import threading
import time

import lumberjack

log = logging.getLogger("pipeline")

TICK = 0.004


def extract(parent: lumberjack.TaskHandle, count: int) -> None:
    # `parent.subtask()`, not a bare `task()`: contextvars propagate into
    # asyncio tasks but *not* into a bare thread, so a worker has to be handed
    # its parent rather than reading one from ambient context.
    with parent.subtask("extract", total=count) as t:
        for i in range(count):
            log.debug("fetched row %d from source table", i)
            t.advance()
            time.sleep(TICK)


def transform(parent: lumberjack.TaskHandle, rows: list[int]) -> None:
    # No count of its own — it is a container for the `track()` below, which
    # does the counting. An uncounted task is indeterminate, not broken.
    with parent.subtask("transform"):
        # `track()` counts for you when you are already iterating something.
        # It takes its total from `len()`, and nests under the subtask above
        # because that subtask is entered — this thread's ambient task.
        for i in lumberjack.track(rows, name="normalize"):
            log.debug("normalized record %d", i)
            if i == len(rows) // 2:
                # WARNING and above is never collapsed: the one line you
                # actually need to see must not be hidden by the thing that
                # hides noise.
                log.warning("row %d had a null timestamp; defaulting to epoch", i)
            time.sleep(TICK * 1.7)


def load(parent: lumberjack.TaskHandle, count: int) -> None:
    with parent.subtask("load", total=count) as t:
        for i in range(count):
            log.debug("wrote batch %d to warehouse", i)
            t.set_progress(i + 1)
            time.sleep(TICK * 2.5)


def main() -> None:
    lumberjack.init()

    with lumberjack.task("etl run") as run:
        workers = [
            threading.Thread(target=extract, args=(run, 700), name="extract"),
            threading.Thread(
                target=transform, args=(run, list(range(450))), name="transform"
            ),
            threading.Thread(target=load, args=(run, 300), name="load"),
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    lumberjack.flush()

    store = lumberjack.current_store()
    if store is None:  # init() ran above
        raise RuntimeError("init() ran, but no store was found")
    # Explicitly unbounded — see the note in demo.py.
    records = store.recent(n=None)
    events = [r for r in records if r.task_event]

    # Display down before the summary goes out — see the note in demo.py.
    lumberjack.shutdown()

    print("\n--- what the instrumentation added ---")
    print(f"records captured  : {len(records)}")
    print(f"of those, task events: {len(events)}")
    print("\nname            parent  final count")
    for row in events:
        if row.task_event != "end":
            continue
        parent = "-" if row.parent_task_id is None else str(row.parent_task_id)
        total = "" if row.progress_total is None else f"/{row.progress_total}"
        print(f"{row.task_label:<15} {parent:<7} {row.progress_current}{total}")

    print(
        "\nProgress here is *exact* — each count is what the code said it was,"
        "\nnot how many times a log line happened to repeat. Ticks are sampled"
        "\nfor the display, but the end row carries the true final count."
    )


if __name__ == "__main__":
    main()
