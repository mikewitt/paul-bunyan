"""Tier 1: the shapes that need real time to happen.

Every test here is `slow` because the thing under test *is* elapsed time —
the legibility threshold is 1.0s and deliberately not settable, and an
inferred total needs a ratio to hold across consecutive real polls. There is
no way to make these fast that does not also stop them testing anything.

Assertions are deliberately loose about numbers and strict about shape. The
display is licensed to be imprecise (Principle 10), so a test demanding an
exact inferred total would be demanding something the design refuses to
promise — and would flake on a loaded runner, which teaches the one habit
this tier exists to prevent.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - drives real child processes
from pathlib import Path

import pytest

from script_runner import ANSI_RE, child_env, needs_rich, run_script

_SCRIPTS = Path(__file__).parent / "scripts"

pytestmark = pytest.mark.slow


def _frame(result: subprocess.CompletedProcess[bytes]) -> list[str]:
    text = ANSI_RE.sub(b"", result.stderr).decode("utf-8", errors="replace")
    return [line for line in re.split(r"[\r\n]+", text) if line.strip()]


def _loop_rows(result: subprocess.CompletedProcess[bytes]) -> list[str]:
    """Rows that count iterations — the loop rows, not the heartbeat."""
    return [line for line in _frame(result) if "iterations" in line]


@pytest.fixture(scope="module")
def slow_loop_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(
        _SCRIPTS, "slow_loop_stages.py", child_env, env={"COLUMNS": "200"}
    )


@pytest.fixture(scope="module")
def nested_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "nested_loops.py", child_env, env={"COLUMNS": "200"})


@pytest.fixture(scope="module")
def workers_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "two_workers.py", child_env, env={"COLUMNS": "200"})


@needs_rich
def test_a_slow_loop_earns_a_position_row(slow_loop_run):
    """At ~1.3s an iteration the loop row alone cannot tell running from hung,
    so a second row counts position within the current iteration. The body has
    five stages and a stable order, so the ordinal is real, not estimated."""
    assert slow_loop_run.returncode == 0, slow_loop_run.stderr
    positions = [line for line in _frame(slow_loop_run) if re.search(r"\d+ of 5", line)]
    assert positions, _frame(slow_loop_run)


@needs_rich
def test_the_same_shape_when_fast_earns_no_position_row(siblings_frame):
    """The other half of the rule, and the reason it is a rule rather than a
    preference: `siblings_merge.py` is the identical structure two hundred
    times faster, and a sub-iteration bar there would be a blur."""
    assert not [line for line in siblings_frame if re.search(r"\d+ of \d+", line)]


@pytest.fixture(scope="module")
def siblings_frame() -> list[str]:
    result = run_script(
        _SCRIPTS, "siblings_merge.py", child_env, env={"COLUMNS": "200"}
    )
    text = ANSI_RE.sub(b"", result.stderr).decode("utf-8", errors="replace")
    return [line for line in re.split(r"[\r\n]+", text) if line.strip()]


@needs_rich
def test_an_inner_loop_is_indented_under_its_parent(nested_run):
    """Depth is derived from observed containment, never declared. Nothing in
    the script says which loop encloses which."""
    assert nested_run.returncode == 0, nested_run.stderr
    rows = _loop_rows(nested_run)
    assert len(rows) == 2, rows
    outer, inner = rows
    assert not outer.startswith(" "), outer
    assert inner.startswith(" "), inner


@needs_rich
def test_an_inner_loop_gets_a_total_nobody_stated(nested_run):
    """The ratio between the enclosing period and the enclosed one *is* the
    total. The script states no total anywhere; 20 is measured.

    Asserted as a band, not a number. A loaded runner shifts the measured
    ratio, and pinning it exactly would make this fail for being right.
    """
    inner = _loop_rows(nested_run)[1]
    match = re.search(r"(\d+)/(\d+)", inner)
    assert match, inner
    total = int(match.group(2))
    assert 15 <= total <= 25, f"inferred total {total} is nowhere near 20: {inner}"


@needs_rich
def test_two_threads_get_two_rows(workers_run):
    """Concurrency falls out of capture rather than being a feature: neither
    thread declares a position or a depth, and attribution happens at write
    time from what `LogRecord` already carries."""
    assert workers_run.returncode == 0, workers_run.stderr
    rows = _loop_rows(workers_run)
    assert len(rows) == 2, rows
    assert any("fetched row" in line for line in rows), rows
    assert any("wrote batch" in line for line in rows), rows
