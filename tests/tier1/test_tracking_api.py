"""Tier 1: the tracking API, from a user's side of it.

Rung 3 of the value ladder, plus the promise that makes rung 3 safe for a
library to reach for — that calling it in an uninitialised application costs
nothing and prints nothing.
"""

from __future__ import annotations

import subprocess  # nosec B404 - drives real child processes
from pathlib import Path

import pytest

from subprocess_rig import child_env, needs_rich, run_script

_SCRIPTS = Path(__file__).parent / "scripts"


@pytest.fixture(scope="module")
def inert_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "tracking_without_init.py", child_env)


@pytest.fixture(scope="module")
def track_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(
        _SCRIPTS, "track_draws_a_bar.py", child_env, env={"COLUMNS": "200"}
    )


def test_the_tracking_api_is_inert_without_init(inert_run):
    """Design Principle 4: a library may call `track()`/`task()`, and absent
    an application's `init()` they produce nothing at all.

    Asserted on stderr rather than a store, because "nothing was written" and
    "nothing was printed" are the same claim when there is no session: an
    OTel-less, uninitialised process has nowhere to put an event. Unmarked —
    inertness owes nothing to rich.
    """
    assert inert_run.returncode == 0, inert_run.stderr.decode(errors="replace")
    assert inert_run.stderr == b"", inert_run.stderr
    assert inert_run.stdout.strip() == b"COMPLETED", inert_run.stdout


@needs_rich
def test_a_stated_total_draws_a_determinate_bar(track_run):
    """`track()` with a total known up front. The final frame is the real
    count, not a sampled one — progress ticks are sampled, `end` is not."""
    stderr = track_run.stderr.decode("utf-8", errors="replace")
    assert "ingest" in stderr, stderr
    assert "50/50" in stderr, stderr


@needs_rich
def test_a_subtask_is_named_alongside_its_parent(track_run):
    """`task()` and `.subtask()` both reach the display under their own
    names — the hierarchy the tracking API states rather than infers."""
    stderr = track_run.stderr.decode("utf-8", errors="replace")
    assert "reconcile" in stderr, stderr
    assert "compare" in stderr, stderr
