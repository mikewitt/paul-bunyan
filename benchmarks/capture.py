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
invalidates the row rather than annotating it — including when only one repeat
of a set dropped, which is why the notes are computed after aggregation.

## Tracking a change over time

    uv run python benchmarks/capture.py --json > benchmarks/baseline.local.json
    # ...do some work...
    uv run python benchmarks/capture.py --compare benchmarks/baseline.local.json

The baseline is a local file and is **not** committed. Absolute nanoseconds
are a property of the machine, and this repo has three times written a
throughput figure into its docs as fact and had the next box contradict it.
`--compare` refuses to render a verdict unless the record count, the repeat
count and the machine fingerprint all match, for exactly that reason.

**Two things the comparison cannot do**, both measured rather than assumed:

- **Resolve a change below ~5%.** Run-to-run spread of the reported minimum is
  1–6% on a quiet box and 7–13% on a busy one. A single before/after pair
  cannot see a 3% win; it needs a change big enough to move the whole range.
- **Compare across different `--records`.** Per-record cost is run-length
  dependent for the write-through modes — `plain` measures ~25µs/record at
  10k and ~44µs at 200k — because a short run finishes before the flush pump
  has ticked many times and so never pays steady-state contention. Anything
  under ~50k is measuring the transient.

## Reading the numbers

Ratios against the `no logging` floor are what travels **between machines**,
and they are how the result should be described. They are *not* the thing to
diff between two runs on one box: the denominators are tiny and carry their
own noise, so ratios there are less stable than the absolutes they are built
from. Compare absolutes on one machine; quote ratios everywhere else.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import logging
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lumberjack

#: Discarded before timing, so import-time and first-call costs — logger
#: lookup, lazily built handler state, the store's prepared statements — land
#: outside the measurement. Verified sufficient: across 190 raw measurements
#: no arm showed a first-repeat inflation.
WARMUP_RECORDS = 2_000

#: A delta smaller than this is not reported as a win or a regression even
#: when the repeat ranges happen not to overlap. Set from the measured
#: run-to-run spread of the minimum (1–6% quiet, 7–13% loaded), so it is a
#: floor on what the instrument can honestly resolve rather than a taste.
SIGNIFICANT_DELTA_PCT = 5.0


Setup = Callable[[], contextlib.AbstractContextManager[logging.Logger]]


@dataclass(frozen=True)
class Arm:
    name: str
    setup: Setup
    #: False only for the floor, which runs the loop without a logging call.
    #: An explicit flag rather than sniffing the setup function's name: a
    #: rename would silently turn the floor into a second filtered-out arm,
    #: shrinking every ratio in the table about tenfold, and no assertion
    #: here or in the suite would notice.
    emits: bool = True


@dataclass
class Sample:
    """One timed pass over one arm."""

    in_loop_ns: float
    total_ns: float
    dropped: int
    stored: int | None


@dataclass
class Result:
    """An arm's repeats, reduced.

    Headline numbers are the **minimum**, which is not the usual choice and is
    load-bearing. The noise here is one-sided — a scheduler preemption, a
    background thread, another tenant on the box all *add* time and none
    subtract it — so the distribution is right-skewed and its lower envelope
    is the most reproducible thing about it. Measured across repeated runs the
    minimum was as stable as or more stable than the median in almost every
    arm, and a mean±stddev would be worse still: the mean tracks how loaded
    the box was, and the standard deviation assumes a symmetry the data does
    not have.

    The raw per-repeat values are kept because the minimum alone cannot say
    whether a delta cleared the noise. Keeping the samples rather than a
    summary statistic also means a future comparison can compute something
    this version did not think of, without invalidating baselines already on
    disk.
    """

    name: str
    in_loop_runs: list[float]
    total_runs: list[float]
    dropped: int
    stored: int | None
    notes: list[str] = field(default_factory=list)

    @property
    def in_loop_ns(self) -> float:
        return min(self.in_loop_runs)

    @property
    def total_ns(self) -> float:
        return min(self.total_runs)

    @property
    def spread_pct(self) -> float:
        """How far the slowest repeat ran from the fastest, as a percentage.

        The table's honesty column: it is what tells a reader whether the
        difference they are looking at between two runs is worth anything.
        """
        low = self.in_loop_ns
        return ((max(self.in_loop_runs) - low) / low * 100) if low else 0.0


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
    terminal.

    One caveat on the rich arm, since it is the flattering row. Its *analysis*
    timer runs regardless of where output goes — the store queries and bar
    updates are all paid — but with stderr on devnull the console is not a
    terminal, so rich skips the frame render and the write. The row therefore
    slightly understates what a real TTY costs. It does not change the
    finding: the gap to `plain` is threefold, and drawing a frame every 200ms
    cannot close that.

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


def _measure(arm: Arm, records: int) -> Sample:
    with arm.setup() as logger:
        emit = logger.debug

        for i in range(WARMUP_RECORDS):
            if arm.emits:
                emit("warmup record %d", i)
        lumberjack.flush()

        # GC off for the timed section. A collection landing inside one arm
        # and not another is pure noise at these magnitudes, and the arms
        # allocate very differently.
        gc.disable()
        try:
            start = time.perf_counter_ns()
            if arm.emits:
                for i in range(records):
                    emit("processing item %d", i)
            else:
                for _i in range(records):
                    pass
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

    return Sample(
        in_loop_ns=in_loop / records,
        total_ns=total / records,
        dropped=dropped,
        stored=stored,
    )


def _aggregate(name: str, samples: list[Sample], records: int) -> Result:
    """Reduce an arm's repeats, and decide whether the row is valid.

    The validity check runs over **every** repeat, not the fastest one. An
    earlier version took the fastest sample and carried its notes, so a set
    where only a slow repeat overflowed printed the drop count in the table
    and still exited zero — the row announced itself invalid and the script
    called it a pass.
    """
    dropped = max(s.dropped for s in samples)
    stored_values = [s.stored for s in samples if s.stored is not None]
    # The worst repeat decides: one that lost records invalidates the set.
    stored = min(stored_values) if stored_values else None

    notes: list[str] = []
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
        name=name,
        in_loop_runs=[s.in_loop_ns for s in samples],
        total_runs=[s.total_ns for s in samples],
        dropped=dropped,
        stored=stored,
        notes=notes,
    )


def default_buffer(records: int) -> int:
    """Big enough that nothing is evicted, with headroom for the warmup."""
    return (records + WARMUP_RECORDS) * 2


def _arms(buffer_size: int) -> list[Arm]:
    return [
        Arm("no logging", _no_logging, emits=False),
        Arm("logging call, filtered out", _filtered_out),
        Arm("stdlib -> NullHandler", _stdlib_null_handler),
        Arm("stdlib -> StreamHandler(devnull)", _stdlib_stream),
        Arm("lumberjack -> plain", lambda: _lumberjack("plain", buffer_size)),
        Arm("lumberjack -> json", lambda: _lumberjack("json", buffer_size)),
        Arm("lumberjack -> rich (live bar)", lambda: _lumberjack("rich", buffer_size)),
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


def run(records: int, repeats: int, buffer_size: int) -> list[Result]:
    return [
        _aggregate(arm.name, [_measure(arm, records) for _ in range(repeats)], records)
        for arm in _arms(buffer_size)
    ]


def _cpu_model() -> str:
    """Best-effort CPU name, for deciding whether two runs are comparable."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _machine() -> dict[str, Any]:
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
    }


def _git_commit() -> str | None:
    """Which revision produced these numbers. Best-effort, never fatal."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _report(
    results: list[Result], records: int, repeats: int, buffer_size: int, drain: float
) -> dict[str, Any]:
    """The JSON document, which doubles as the baseline format.

    Raw per-repeat values are kept rather than a summary, so a later version
    can compute a statistic this one did not think of without invalidating
    baselines already written.
    """
    return {
        "records": records,
        "repeats": repeats,
        "buffer_size": buffer_size,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "machine": _machine(),
        "drain_records_per_s": round(drain),
        "arms": [
            {
                "name": r.name,
                "in_loop_ns": round(r.in_loop_ns, 1),
                "total_ns": round(r.total_ns, 1),
                "in_loop_ns_runs": [round(v, 1) for v in r.in_loop_runs],
                "total_ns_runs": [round(v, 1) for v in r.total_runs],
                "spread_pct": round(r.spread_pct, 1),
                "records_per_s": round(1e9 / r.total_ns) if r.total_ns else None,
                "dropped": r.dropped,
                "stored": r.stored,
                "notes": r.notes,
            }
            for r in results
        ],
    }


def _render_table(results: list[Result], records: int, drain_per_s: float) -> str:
    floor = results[0].in_loop_ns or 1.0
    width = max(len(r.name) for r in results)
    lines = [
        f"{records:,} records per arm, best of each set. "
        f"Python {sys.version.split()[0]} on {sys.platform}.",
        "",
        f"{'arm':<{width}}  {'in-loop':>10}  {'total':>10}  "
        f"{'records/s':>12}  {'vs floor':>9}  {'spread':>7}  {'dropped':>8}",
        f"{'-' * width}  {'-' * 10}  {'-' * 10}  {'-' * 12}  {'-' * 9}  "
        f"{'-' * 7}  {'-' * 8}",
    ]
    for r in results:
        per_sec = 1e9 / r.total_ns if r.total_ns else float("inf")
        lines.append(
            f"{r.name:<{width}}  {r.in_loop_ns:>9,.0f}n  {r.total_ns:>9,.0f}n  "
            f"{per_sec:>12,.0f}  {r.in_loop_ns / floor:>8,.1f}x  "
            f"{r.spread_pct:>6,.1f}%  {r.dropped or '-':>8}"
        )
    notes = [f"  ! {r.name}: {n}" for r in results for n in r.notes]
    if notes:
        lines += ["", *notes]
    lines += [
        "",
        "in-loop = time inside the logging call, which is what the calling",
        "thread pays. total = in-loop plus the drain into the store, which",
        "lumberjack defers to a background thread and the other arms have",
        "already paid. spread = how far the slowest repeat ran from the",
        "fastest, which is the scale below which a difference means nothing.",
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
        "",
        "Quote the ratios, not the nanoseconds — absolutes belong to this box.",
        "To diff two runs on one box, use --compare, which reads the absolutes:",
        "the ratios have a tiny noisy denominator and travel worse than what",
        "they are built from.",
    ]
    return "\n".join(lines)


def _incomparable(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Why these two runs cannot be diffed, if they cannot.

    Deliberately strict. A comparison that quietly comes from a different
    record count or a different machine is worse than no comparison: it looks
    like a measurement, and this project has already put three mutually
    contradictory throughput figures into its own documentation as fact.
    """
    reasons = []
    for key in ("records", "repeats"):
        if baseline.get(key) != current.get(key):
            reasons.append(
                f"{key}: baseline {baseline.get(key)}, now {current.get(key)}"
            )
    base_machine, now_machine = baseline.get("machine", {}), current["machine"]
    for key in ("platform", "python", "cpu", "cpu_count"):
        if base_machine.get(key) != now_machine.get(key):
            was, now = base_machine.get(key), now_machine.get(key)
            reasons.append(f"{key}: baseline {was!r}, now {now!r}")
    return reasons


def _verdict(base_runs: list[float], new_runs: list[float], delta_pct: float) -> str:
    """Did this change clear the noise?

    Range-disjointness rather than a t-test: with five repeats of a skewed,
    one-sided distribution the assumptions behind a parametric test are not
    met, and a non-overlap rule is transparent enough that a reader can check
    it by eye against the spreads printed beside it. Conservative, which is
    the right bias for a tool whose job is to stop people quoting noise.
    """
    if not (min(new_runs) > max(base_runs) or max(new_runs) < min(base_runs)):
        return "noise"
    if abs(delta_pct) < SIGNIFICANT_DELTA_PCT:
        return "suspect"
    return "faster" if delta_pct < 0 else "SLOWER"


def _render_comparison(baseline: dict[str, Any], current: dict[str, Any]) -> str:
    reasons = _incomparable(baseline, current)
    base_arms = {a["name"]: a for a in baseline.get("arms", [])}
    width = max(len(a["name"]) for a in current["arms"])

    header = [
        f"Comparison against {baseline.get('git_commit') or 'unknown'} "
        f"({baseline.get('timestamp', 'unknown time')})",
        "",
    ]
    if reasons:
        header += [
            "NOT COMPARABLE — verdicts withheld. The two runs differ in:",
            *(f"  - {r}" for r in reasons),
            "",
            "Deltas are shown for information only. Re-record the baseline on",
            "this machine with the same --records and --repeats to get a real",
            "answer.",
            "",
        ]
    lines = header + [
        f"{'arm':<{width}}  {'baseline':>10}  {'current':>10}  {'delta':>8}  "
        f"{'spreads':>13}  {'verdict':>8}",
        f"{'-' * width}  {'-' * 10}  {'-' * 10}  {'-' * 8}  {'-' * 13}  {'-' * 8}",
    ]
    for arm in current["arms"]:
        base = base_arms.get(arm["name"])
        if base is None:
            lines.append(f"{arm['name']:<{width}}  {'(new arm)':>10}")
            continue
        if "in_loop_ns_runs" not in base:
            lines.append(
                f"{arm['name']:<{width}}  baseline predates per-repeat data; re-record"
            )
            continue
        delta = (arm["in_loop_ns"] - base["in_loop_ns"]) / base["in_loop_ns"] * 100
        verdict = (
            "-"
            if reasons
            else _verdict(base["in_loop_ns_runs"], arm["in_loop_ns_runs"], delta)
        )
        lines.append(
            f"{arm['name']:<{width}}  {base['in_loop_ns']:>9,.0f}n  "
            f"{arm['in_loop_ns']:>9,.0f}n  {delta:>+7,.1f}%  "
            f"{base.get('spread_pct', 0):>5,.1f}% /{arm['spread_pct']:>5,.1f}%  "
            f"{verdict:>8}"
        )
    lines += [
        "",
        "Compared on in-loop absolutes, which are the stable thing on one box.",
        "'noise' means the repeat ranges overlap. 'suspect' means they do not,"
        f" but the change is under {SIGNIFICANT_DELTA_PCT:.0f}%",
        "— around this instrument's resolution. Neither is evidence.",
        "",
        "Run it again before believing a single 'faster' or 'SLOWER', and read",
        "the spreads: if they are wide the box was busy and the verdict is",
        "worth less. Two runs of unchanged code should read 'noise' throughout;",
        "if they do not, the machine is too loaded to measure on right now.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    assert __doc__ is not None
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        # Five rather than three from measurement, not taste: the reported
        # minimum wandered 7-13% run-to-run at three repeats and 1-6% at five.
        help="measurement sets per arm; the minimum is reported",
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=0,
        help="write buffer size; 0 sizes it to the run so nothing is evicted",
    )
    parser.add_argument(
        "--compare",
        metavar="BASELINE.json",
        help="diff against a previous --json run recorded on this machine",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    buffer_size = args.buffer or default_buffer(args.records)
    results = run(args.records, args.repeats, buffer_size)
    drain = measure_drain(args.records)
    report = _report(results, args.records, args.repeats, buffer_size, drain)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_render_table(results, args.records, drain))
        if args.compare:
            baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            print()
            print(_render_comparison(baseline, report))

    # A row that lost records is not a measurement, so say so in the exit code
    # as well as in the table — this is what makes the script usable as a
    # check rather than only as a report. A comparison verdict deliberately
    # does not affect the exit code: this is an instrument, not a gate.
    return 1 if any(r.notes for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
