"""The benchmark must keep running, or it stops being a regression signal.

`benchmarks/capture.py` is not shipped and not imported by anything, so
nothing else in the suite notices when `init()` grows a keyword or `flush()`
changes shape — the script just breaks, quietly, until somebody next runs it
by hand and finds a number they cannot compare against the last one.

The subprocess tests run it at a deliberately tiny record count. They are not
measuring anything: at 200 records the numbers are noise, and asserting on
them would make the suite fail on a busy CI runner. What they assert is that
the script executes end to end against the current API and reports no lost
records — which is also the one benchmark result that *is* a correctness claim
rather than a performance one (Principle 6: the buffer->store path must not
drop).

The in-process tests below them cover the part that decides whether a row is
*valid*, driven with stub samples. That logic has already been wrong once, in
a way no end-to-end run could catch: a set where only a slow repeat overflowed
printed the drop count and still exited zero.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_BENCHMARKS = Path(__file__).resolve().parent.parent / "benchmarks"
_SCRIPT = _BENCHMARKS / "capture.py"


def _load_capture() -> Any:
    """Import the script as a module — `benchmarks/` is not a package."""
    spec = importlib.util.spec_from_file_location("bench_capture", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`, which is None for a module still being
    # built, and raises rather than degrading.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture = _load_capture()


@pytest.fixture(scope="module")
def benchmark_json() -> dict:
    """Run the benchmark once and hand its JSON to every test below."""
    env = dict(os.environ)
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join([src_dir, env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--records", "200", "--repeats", "2", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"benchmark exited {result.returncode} — a non-zero exit means it "
        f"lost records.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_every_arm_reports_a_measurement(benchmark_json: dict) -> None:
    arms = benchmark_json["arms"]
    # The floor, the two stdlib comparisons and the three output modes. A
    # dropped arm would silently shrink the table rather than fail anything.
    assert len(arms) == 7
    assert arms[0]["name"] == "no logging"
    for arm in arms:
        assert arm["in_loop_ns"] > 0, arm
        assert arm["total_ns"] >= arm["in_loop_ns"], arm


def test_no_records_are_lost(benchmark_json: dict) -> None:
    """The one assertion here that is about correctness, not speed."""
    for arm in benchmark_json["arms"]:
        assert arm["dropped"] == 0, arm
        assert arm["notes"] == [], arm


def test_lumberjack_arms_store_everything_they_emit(benchmark_json: dict) -> None:
    """`dropped == 0` only says the buffer held; this says the store has them."""
    lumberjack_arms = [a for a in benchmark_json["arms"] if a["stored"] is not None]
    assert len(lumberjack_arms) == 3
    for arm in lumberjack_arms:
        # Warmup records are captured too, and deliberately: they prove the
        # store kept everything, not just what the timed section emitted.
        assert arm["stored"] > benchmark_json["records"], arm


def test_the_floor_is_the_cheapest_arm(benchmark_json: dict) -> None:
    """A sanity check on the harness rather than on lumberjack.

    If doing nothing is not the fastest thing measured, the timing loop is
    wrong and every ratio in the table is meaningless.
    """
    arms = benchmark_json["arms"]
    floor = arms[0]["in_loop_ns"]
    assert all(a["in_loop_ns"] >= floor for a in arms)


def test_the_baseline_format_carries_what_a_comparison_needs(
    benchmark_json: dict,
) -> None:
    """The JSON doubles as the baseline file, so its shape is a contract.

    Raw per-repeat values are the load-bearing part: without them `--compare`
    cannot tell a real change from a noisy one, and a baseline recorded today
    becomes useless to a smarter comparison written later.
    """
    assert benchmark_json["machine"]["cpu_count"]
    assert benchmark_json["repeats"] == 2
    for arm in benchmark_json["arms"]:
        assert len(arm["in_loop_ns_runs"]) == 2, arm
        assert len(arm["total_ns_runs"]) == 2, arm
        # The headline is the minimum of the samples behind it, not a
        # separately computed number that could drift from them.
        assert arm["in_loop_ns"] == pytest.approx(min(arm["in_loop_ns_runs"]), rel=1e-6)


def _samples(*specs: tuple[float, int]) -> list[Any]:
    """Stub repeats as (per-record ns, dropped) pairs."""
    return [
        capture.Sample(ns, ns, dropped=dropped, stored=1 if dropped else 2_200)
        for ns, dropped in specs
    ]


def test_a_drop_in_a_slower_repeat_still_invalidates_the_row() -> None:
    """The regression that made the script a report rather than a check.

    Aggregation used to keep the fastest repeat's *notes* while taking the
    worst repeat's *dropped* count, so this row printed `dropped=500` and the
    script exited 0.
    """
    result = capture._aggregate("arm", _samples((10.0, 0), (99.0, 500)), records=200)

    assert result.dropped == 500
    assert result.notes, "a row that lost records must be annotated"
    assert "DROPPED" in result.notes[0]


def test_a_short_store_is_caught_even_without_eviction() -> None:
    """`dropped == 0` is the weaker check; this is the one that matters."""
    short = [capture.Sample(10.0, 10.0, dropped=0, stored=5)]
    result = capture._aggregate("arm", short, records=200)

    assert result.dropped == 0
    assert any("stored" in n for n in result.notes)


def test_a_clean_set_is_not_annotated() -> None:
    """Guards the two tests above from passing by always finding fault."""
    clean = _samples((10.0, 0), (12.0, 0))
    result = capture._aggregate("arm", clean, records=200)

    assert result.notes == []
    assert result.in_loop_ns == 10.0
    # 10 -> 12 is a fifth slower, and the column exists to say so.
    assert result.spread_pct == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("base", "new", "expected"),
    [
        # Ranges overlap, so nothing can be concluded however big the gap.
        ([100.0, 130.0], [120.0, 150.0], "noise"),
        # Disjoint but under the resolution floor: real, and still not worth
        # reporting as a win.
        ([100.0, 101.0], [102.0, 103.0], "suspect"),
        ([100.0, 101.0], [150.0, 155.0], "SLOWER"),
        ([150.0, 155.0], [100.0, 101.0], "faster"),
    ],
)
def test_verdicts_require_the_ranges_to_separate(
    base: list[float], new: list[float], expected: str
) -> None:
    delta = (min(new) - min(base)) / min(base) * 100
    assert capture._verdict(base, new, delta) == expected


def test_a_baseline_from_another_machine_is_refused() -> None:
    """The whole reason the machine fingerprint is recorded.

    A comparison that silently spans two boxes looks like a measurement and
    is not one — the failure this repo has already made three times in prose.
    """
    current = {"records": 1000, "repeats": 5, "machine": capture._machine()}
    baseline = {
        "records": 1000,
        "repeats": 5,
        "machine": {**capture._machine(), "cpu": "a different chip"},
    }
    reasons = capture._incomparable(baseline, current)

    assert any("cpu" in r for r in reasons)


def test_a_baseline_at_a_different_record_count_is_refused() -> None:
    """Per-record cost is run-length dependent, so this is not pedantry.

    The write-through modes measure roughly 25us/record at 10k and 44us at
    200k, because a short run finishes before the pump has ticked many times.
    Diffing across counts would read that as a 75% regression.
    """
    machine = capture._machine()
    reasons = capture._incomparable(
        {"records": 10_000, "repeats": 5, "machine": machine},
        {"records": 200_000, "repeats": 5, "machine": machine},
    )

    assert any("records" in r for r in reasons)


def test_matching_runs_are_comparable() -> None:
    """Guards the two refusal tests from passing by refusing everything."""
    same = {"records": 1000, "repeats": 5, "machine": capture._machine()}
    assert capture._incomparable(dict(same), dict(same)) == []
