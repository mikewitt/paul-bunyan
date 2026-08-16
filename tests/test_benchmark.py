"""The benchmark must keep running, or it stops being a regression signal.

`benchmarks/capture.py` is not shipped and not imported by anything, so
nothing else in the suite notices when `init()` grows a keyword or `flush()`
changes shape — the script just breaks, quietly, until somebody next runs it
by hand and finds a number they cannot compare against the last one.

This runs it at a deliberately tiny record count. It is not measuring
anything: at 200 records the numbers are noise, and asserting on them would
make the suite fail on a busy CI runner. What it asserts is that the script
executes end to end against the current API and reports no lost records —
which is also the one benchmark result that *is* a correctness claim rather
than a performance one (Principle 6: the buffer->store path must not drop).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "benchmarks" / "capture.py"


@pytest.fixture(scope="module")
def benchmark_json() -> dict:
    """Run the benchmark once and hand its JSON to every test below."""
    env = dict(os.environ)
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join([src_dir, env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--records", "200", "--repeats", "1", "--json"],
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
    assert [a["name"] for a in arms][0] == "no logging"
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
