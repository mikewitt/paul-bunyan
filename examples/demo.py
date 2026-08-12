"""A runnable demonstration of what lumberjack does to ordinary log output.

Run it on a real terminal to see the point:

    uv run python examples/demo.py

Three worker threads each log a line per iteration — roughly 1500 records in
total, the sort of `logger.debug(...)` spam a developer writes while building
something and then deletes once it works. Instead of scrolling past, each
repeating log site becomes a bar that advances in place.

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


def main() -> None:
    # The only lumberjack-aware line in the program.
    lumberjack.init(level=logging.DEBUG)

    random.seed(0)
    workers = [
        threading.Thread(target=extract, args=(700,), name="extract"),
        threading.Thread(target=transform, args=(450,), name="transform"),
        threading.Thread(target=load, args=(300,), name="load"),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    # Drain anything the periodic pump hasn't picked up yet, so the summary
    # below reflects the complete run.
    lumberjack.flush()

    store = lumberjack.current_store()
    records = store.recent()

    print("\n--- the display was lossy; the store was not ---")
    print(f"records captured : {len(records)}")
    print(f"renderer         : {type(lumberjack.current_renderer()).__name__}")
    print(f"output mode      : {lumberjack.current_output_mode()}")

    print("\nrecords per log site (this grouping is what drives the bars):")
    for source, count in sorted(store.count_by_source().items(), key=lambda kv: -kv[1]):
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

    lumberjack.shutdown()


if __name__ == "__main__":
    main()
