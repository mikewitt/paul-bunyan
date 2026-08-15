"""A runnable demonstration of what lumberjack does to ordinary log output.

Run it on a real terminal to see the point:

    uv run python examples/demo.py

Four worker threads log a line per iteration — roughly 2000 records in total,
the sort of `logger.debug(...)` spam a developer writes while building
something and then deletes once it works. Instead of scrolling past, each
repeating log site becomes a bar that advances in place.

Three of the workers run flat loops, so their bars pulse: nothing in the
stream says how long they are. The fourth nests a loop inside a loop, and
that *is* visible in the stream — the inner line fires twenty times per outer
iteration — so its bar promotes itself to a real percentage and indents
under its parent, with nobody having written a single line of instrumentation.

To see what it replaces, force the plain renderer and watch the same run
scroll by:

    LUMBERJACK_OUTPUT_MODE=plain uv run python examples/demo.py

    # PowerShell:
    $env:LUMBERJACK_OUTPUT_MODE="plain"; uv run python examples/demo.py

Nothing about the worker functions changes between those two runs. They call
stdlib `logging` and know nothing about lumberjack — which is the entire
premise: the display is a property of how the application was configured, not
of how the library was written.
"""

from __future__ import annotations

import logging
import random
import threading
import time

import lumberjack

log = logging.getLogger("pipeline")

# Slow enough that the bars visibly move rather than finishing instantly.
TICK = 0.004


def extract(count: int) -> None:
    for i in range(count):
        log.debug("fetched row %d from source table", i)
        time.sleep(TICK)


def transform(count: int) -> None:
    for i in range(count):
        log.debug("normalized record %d", i)
        if i == count // 2:
            # WARNING and above is never collapsed — it prints above the bars,
            # because the one line you actually need to see must not be hidden
            # by the thing that hides noise.
            log.warning("row %d had a null timestamp; defaulting to epoch", i)
        time.sleep(TICK * 1.7)


def load(count: int) -> None:
    for i in range(count):
        log.debug("wrote batch %d to warehouse", i)
        time.sleep(TICK * 2.5)


def reconcile(batches: int, rows: int) -> None:
    """A genuinely nested loop, still saying nothing about lumberjack.

    Nothing here declares a total, a name, or a hierarchy. The inner line
    fires `rows` times between consecutive firings of the outer one, and that
    ratio is both the evidence the loops are nested and the length of the
    inner one — so after a couple of outer iterations the inner line stops
    being a counter and becomes a real bar that fills, resets, and fills
    again, indented under its parent.
    """
    for batch in range(batches):
        log.debug("reconciling batch %d", batch)
        for row in range(rows):
            log.debug("compared row %d against ledger", row)
            time.sleep(TICK * 1.25)


def main() -> None:
    # The only lumberjack-aware line in the program.
    lumberjack.init()

    random.seed(0)
    workers = [
        threading.Thread(target=extract, args=(700,), name="extract"),
        threading.Thread(target=transform, args=(450,), name="transform"),
        threading.Thread(target=load, args=(300,), name="load"),
        threading.Thread(target=reconcile, args=(24, 20), name="reconcile"),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    # Drain anything the periodic pump hasn't picked up yet, so the summary
    # below reflects the complete run.
    lumberjack.flush()

    # The accessors return None before init() and after shutdown(), so a
    # type checker will make you say why you know better. init() ran above.
    store = lumberjack.current_store()
    assert store is not None
    records = store.recent()
    by_source = sorted(store.count_by_source().items(), key=lambda kv: -kv[1])
    renderer_name = type(lumberjack.current_renderer()).__name__
    mode = lumberjack.current_output_mode()

    # Everything the summary needs is now in local variables, so the display
    # comes down *before* a line of it is printed. A program's own output and
    # a live redraw must not share a terminal: lumberjack deliberately leaves
    # stdout alone, so nothing is there to interleave the two politely, and
    # printing over a live frame is how a summary ends up shredded.
    lumberjack.shutdown()

    print("\n--- the display was lossy; the store was not ---")
    print(f"records captured : {len(records)}")
    print(f"renderer         : {renderer_name}")
    print(f"output mode      : {mode}")

    print("\nrecords per log site (this grouping is what drives the bars):")
    for source, count in by_source:
        print(f"  {source.func_name:<12} line {source.lineno:<4} {count:>5} records")

    print("\nrecords per thread (attributed at write time, never inferred):")
    by_thread: dict[str, int] = {}
    for record in records:
        by_thread[record.thread_name] = by_thread.get(record.thread_name, 0) + 1
    for name, count in sorted(by_thread.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<12} {count:>5} records")

    warnings = [r for r in records if r.level_no >= logging.WARNING]
    print(f"\nwarnings (shown above the bars, and still stored): {len(warnings)}")
    for record in warnings:
        print(f"  {record.level_name} {record.message}")


if __name__ == "__main__":
    main()
