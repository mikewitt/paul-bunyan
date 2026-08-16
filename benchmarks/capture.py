"""What does it cost to leave the debug logging in?

lumberjack's whole pitch is that verbose logging is affordable — so the number
that matters is the per-record cost on the capture path, measured against the
alternatives a developer actually has.

    uv run python benchmarks/capture.py
    uv run python benchmarks/capture.py --records 200000 --json

## What is measured, and why it is two numbers

**In-loop** is the time the calling thread spends inside `logger.debug(...)`.
It is what a developer feels, and it is the honest answer to "how much slower
is my loop".

**Total** adds the drain that gets records into the store, forced with a final
`flush()`. lumberjack deliberately moves that work onto a background thread,
so in-loop understates the true cost and total overstates the felt one. Both
are reported because quoting either alone is misleading.

## Dropped records are part of the result

The write buffer is bounded and evicts under pressure. A throughput figure
from a run that dropped records is measuring how fast lumberjack can throw
data away, so `dropped` is printed on every row and a non-zero value
invalidates the row rather than annotating it.

## Reading the numbers

Ratios against the `no logging` floor are the durable part. Absolute
nanoseconds move with the machine, the Python build and the weather; the
*relationship* between the arms is what should hold, and is what a regression
would show up in.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import lumberjack

#: Discarded before timing, so import-time and first-call costs — logger
#: lookup, lazily built handler state, the store's prepared statements — land
#: outside the measurement.
WARMUP_RECORDS = 2_000


@dataclass
class Result:
    name: str
    in_loop_ns: float
    total_ns: float
    dropped: int
    stored: int | None = None
    notes: list[str] = field(default_factory=list)


def _reset_logging() -> None:
    """Put the root logger back to a known state between arms."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)


@contextlib.contextmanager
def _no_logging() -> Iterator[logging.Logger]:
    """The floor: the loop body with no logging call at all.

    Measured with a logger object in hand but never called, so the arms differ
    only in what happens per iteration.
    """
    _reset_logging()
    yield logging.getLogger("bench.none")


@contextlib.contextmanager
def _filtered_out() -> Iterator[logging.Logger]:
    """Calls left in the source, discarded by the level.

    The state a developer is in when they *have* followed the usual advice:
    the `logger.debug(...)` lines are still there, and stdlib throws them away
    before building a record. This is the number lumberjack has to justify
    itself against, not the one below it.
    """
    _reset_logging()
    logger = logging.getLogger("bench.filtered")
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    yield logger


@contextlib.contextmanager
def _stdlib_null_handler() -> Iterator[logging.Logger]:
    """Record built and dispatched, then discarded.

    Isolates stdlib's own record-creation cost from anything a handler does
    with it, which is the fairest floor for "what does capture cost".
    """
    _reset_logging()
    logger = logging.getLogger("bench.nullhandler")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(logging.NullHandler())
    try:
        yield logger
    finally:
        logger.handlers.clear()


@contextlib.contextmanager
def _stdlib_stream() -> Iterator[logging.Logger]:
    """Formatted and written out — what logging to a file actually costs.

    The realistic comparison: this is roughly what a developer has today if
    they kept their debug lines and pointed them somewhere.
    """
    _reset_logging()
    logger = logging.getLogger("bench.stream")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    sink = open(os.devnull, "w")
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        logger.handlers.clear()
        sink.close()


@contextlib.contextmanager
def _lumberjack(output_mode: str, buffer_size: int) -> Iterator[logging.Logger]:
    """lumberjack as an application would actually install it.

    stderr goes to devnull for the duration: the plain renderer is
    write-through, so leaving it pointed at a terminal would benchmark the
    terminal. The rich arm redraws on its own timer regardless of where it
    writes, which is the property its row exists to show.

    **The buffer is sized to the run, and that is not cheating.** At the
    default 10,000 a fast loop outruns the pump and the buffer starts
    evicting, at which point the row measures how quickly lumberjack can
    discard a record rather than what it costs to capture one. Those are two
    different questions and both are worth answering — the drain ceiling is
    measured separately below, where it is the subject rather than a
    contaminant.
    """
    _reset_logging()
    real_stderr = sys.stderr
    sink = open(os.devnull, "w")
    sys.stderr = sink
    try:
        lumberjack.init(
            output_mode=output_mode, level=logging.DEBUG, buffer_size=buffer_size
        )
        yield logging.getLogger("bench.lumberjack")
    finally:
        lumberjack.shutdown()
        sys.stderr = real_stderr
        sink.close()


def _measure(
    setup: Callable[[], contextlib.AbstractContextManager[logging.Logger]],
    records: int,
) -> Result:
    notes: list[str] = []
    with setup() as logger:
        emit = logger.debug
        silent = setup.__name__ == "_no_logging"

        for i in range(WARMUP_RECORDS):
            if not silent:
                emit("warmup record %d", i)
        lumberjack.flush()

        # GC off for the timed section. A collection landing inside one arm
        # and not another is pure noise at these magnitudes, and the arms
        # allocate very differently.
        gc.disable()
        try:
            start = time.perf_counter_ns()
            if silent:
                for _i in range(records):
                    pass
            else:
                for i in range(records):
                    emit("processing item %d", i)
            in_loop = time.perf_counter_ns() - start

            # The drain lumberjack defers to its pump. A no-op for every
            # other arm, which is the point: they have already paid it.
            lumberjack.flush()
            total = time.perf_counter_ns() - start
        finally:
            gc.enable()

        handler = lumberjack.current_handler()
        dropped = handler.dropped if handler is not None else 0
        store = lumberjack.current_store()
        # Aggregate rather than `recent(n=None)`: materializing every row to
        # count it would be slower than the run it is checking.
        stored = sum(store.count_by_source().values()) if store is not None else None

    if dropped:
        notes.append(
            f"DROPPED {dropped:,} records — the buffer overflowed, so this "
            "row measures discard speed, not capture speed"
        )
    # Stronger than `dropped == 0`, which only says the buffer never evicted.
    # This says the records reached the store, which is the promise being
    # benchmarked: Principle 6's buffer->store path must not lose any.
    expected = records + WARMUP_RECORDS
    if stored is not None and stored != expected:
        notes.append(
            f"stored {stored:,} of {expected:,} — records went missing "
            "somewhere other than buffer eviction"
        )
    return Result(
        name="",
        in_loop_ns=in_loop / records,
        total_ns=total / records,
        dropped=dropped,
        stored=stored,
        notes=notes,
    )


Setup = Callable[[], contextlib.AbstractContextManager[logging.Logger]]


def _arms(buffer_size: int) -> list[tuple[str, Setup]]:
    return [
        ("no logging", _no_logging),
        ("logging call, filtered out", _filtered_out),
        ("stdlib -> NullHandler", _stdlib_null_handler),
        ("stdlib -> StreamHandler(devnull)", _stdlib_stream),
        ("lumberjack -> plain", lambda: _lumberjack("plain", buffer_size)),
        ("lumberjack -> json", lambda: _lumberjack("json", buffer_size)),
        ("lumberjack -> rich (live bar)", lambda: _lumberjack("rich", buffer_size)),
    ]


def measure_drain(records: int) -> float:
    """Records per second from the write buffer into the store.

    The ceiling that decides whether a given log rate is sustainable. Measured
    with the pump switched off (`flush_interval=0`) and one explicit `flush()`,
    so it times the drain itself rather than however many times a timer
    happened to fire.

    **This is the batched best case, and the real pump will not reach it.**
    One `flush()` of the whole run is a single large insert; the pump wakes on
    a timer and drains whatever has arrived, so it pays the per-batch overhead
    far more often. Read this as an upper bound rather than a rate to plan
    against.

    A loop that emits faster than this does not slow down — it fills the
    bounded buffer and the oldest records are evicted. That is the design
    (Principle 6 protects the store, not the display), but it means this
    number, not the per-record cost above, is what says whether records will
    survive.
    """
    _reset_logging()
    real_stderr, sink = sys.stderr, open(os.devnull, "w")
    sys.stderr = sink
    try:
        lumberjack.init(
            output_mode="plain",
            level=logging.DEBUG,
            buffer_size=records * 2,
            flush_interval=0,
        )
        logger = logging.getLogger("bench.drain")
        for i in range(records):
            logger.debug("processing item %d", i)
        start = time.perf_counter_ns()
        lumberjack.flush()
        elapsed = time.perf_counter_ns() - start
    finally:
        lumberjack.shutdown()
        sys.stderr = real_stderr
        sink.close()
    return records / (elapsed / 1e9)


def default_buffer(records: int) -> int:
    """Big enough that nothing is evicted, with headroom for the warmup."""
    return (records + WARMUP_RECORDS) * 2


def run(records: int, repeats: int, buffer_size: int) -> list[Result]:
    results: list[Result] = []
    for name, setup in _arms(buffer_size):
        runs = [_measure(setup, records) for _ in range(repeats)]
        # Minimum, not mean. Every source of noise here adds time — a
        # scheduler preemption, a background thread, another tenant on the
        # box — so the fastest run is the closest to the real cost.
        best = min(runs, key=lambda r: r.in_loop_ns)
        best.name = name
        best.total_ns = min(r.total_ns for r in runs)
        best.dropped = max(r.dropped for r in runs)
        results.append(best)
    return results


def _render_table(results: list[Result], records: int, drain_per_s: float) -> str:
    floor = results[0].in_loop_ns or 1.0
    width = max(len(r.name) for r in results)
    lines = [
        f"{records:,} records per arm, best of each set. "
        f"Python {sys.version.split()[0]} on {sys.platform}.",
        "",
        f"{'arm':<{width}}  {'in-loop':>10}  {'total':>10}  "
        f"{'records/s':>12}  {'vs floor':>9}  {'dropped':>8}",
        f"{'-' * width}  {'-' * 10}  {'-' * 10}  {'-' * 12}  {'-' * 9}  {'-' * 8}",
    ]
    for r in results:
        per_sec = 1e9 / r.total_ns if r.total_ns else float("inf")
        lines.append(
            f"{r.name:<{width}}  {r.in_loop_ns:>9,.0f}n  {r.total_ns:>9,.0f}n  "
            f"{per_sec:>12,.0f}  {r.in_loop_ns / floor:>8,.1f}x  "
            f"{r.dropped or '-':>8}"
        )
    notes = [f"  ! {r.name}: {n}" for r in results for n in r.notes]
    if notes:
        lines += ["", *notes]
    lines += [
        "",
        "in-loop = time inside the logging call, which is what the calling",
        "thread pays. total = in-loop plus the drain into the store, which",
        "lumberjack defers to a background thread and the other arms have",
        "already paid. Ratios travel between machines; nanoseconds do not.",
        "",
        "The live rich display is the *cheapest* of the three lumberjack rows,",
        "which is the whole design showing up in a measurement. A write-through",
        "renderer runs synchronously inside emit(), so plain and json format and",
        "write every record on the calling thread. The live bar declares itself",
        "lossy, does nothing per record, and redraws on a ~200ms timer instead —",
        "so its cost is set by how many bars there are, not by log volume.",
        "",
        f"buffer -> store drain: {drain_per_s:,.0f} records/s.",
        "",
        "The rate above which records stop surviving. A loop emitting faster",
        "than this does not block — it fills the bounded buffer and the oldest",
        "records are evicted. Treat it as an upper bound: it is one flush of",
        "the whole run, where the real pump pays per-batch overhead on every",
        "timer tick. The arms above are given a buffer sized to the run, so",
        "they measure capture cost rather than eviction cost.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument(
        "--repeats", type=int, default=3, help="measurement sets per arm; best wins"
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=0,
        help="write buffer size; 0 sizes it to the run so nothing is evicted",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    buffer_size = args.buffer or default_buffer(args.records)
    results = run(args.records, args.repeats, buffer_size)
    drain_per_s = measure_drain(args.records)
    if args.json:
        print(
            json.dumps(
                {
                    "records": args.records,
                    "repeats": args.repeats,
                    "buffer_size": buffer_size,
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "drain_records_per_s": round(drain_per_s),
                    "arms": [
                        {
                            "name": r.name,
                            "in_loop_ns": round(r.in_loop_ns, 1),
                            "total_ns": round(r.total_ns, 1),
                            "records_per_s": (
                                round(1e9 / r.total_ns) if r.total_ns else None
                            ),
                            "dropped": r.dropped,
                            "stored": r.stored,
                            "notes": r.notes,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        print(_render_table(results, args.records, drain_per_s))

    # A row that lost records is not a measurement, so say so in the exit code
    # as well as in the table — this is what makes the script usable as a
    # check rather than only as a report.
    return 1 if any(r.notes for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
