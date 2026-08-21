"""Tier 1: the store keeps a window, not a transcript.

Principle 6's lossless half is pinned by `test_logging_becomes_progress.py`:
every record captured reaches the store. This is the other half of the same
sentence — what the store *retains* is a stated bound rather than however
long the process happens to run.

The claim is about the store and is read back through `current_store()`, not
off the screen. The child forces `rich` only so a hundred thousand lines do
not have to travel down a pipe to prove something about a database.
"""

from __future__ import annotations

import subprocess  # nosec B404 - drives a real child process
from pathlib import Path

import pytest

from script_runner import child_env, run_script

_SCRIPTS = Path(__file__).parent / "scripts"

#: Mirrors the script's own constants. Written out rather than imported: a
#: tier-1 parent may not import `lumberjack`, and the script may not import
#: `lumberjack.store`, so the number a user would type is the number both
#: sides state.
_RETAIN = 10_000
_WRITTEN = _RETAIN * 3


@pytest.fixture(scope="module")
def run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "bounded_store.py", child_env, env={"COLUMNS": "200"})


def _reported(result: subprocess.CompletedProcess[bytes]) -> dict[str, str]:
    lines = result.stdout.decode().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_the_store_stops_growing(run):
    """The defect: nothing called `evict()`, so a `:memory:` store
    accumulated every record for the life of the process — 319 bytes each,
    measured, linear, with no plateau."""
    reported = _reported(run)
    assert int(reported["WRITTEN"]) == _WRITTEN
    assert int(reported["HELD"]) <= _RETAIN + _RETAIN // 10


def test_the_bound_came_from_retention_and_not_from_a_full_buffer(run):
    """Two very different things bound this number, and only one is the
    feature.

    The write buffer evicts under pressure and reports how many it lost. A
    run that dropped records there would show a bounded store for a reason
    that has nothing to do with retention, and would pass the test above.
    """
    reported = _reported(run)
    assert int(reported["DROPPED"]) == 0
    assert int(reported["HELD"]) >= _RETAIN, "the store kept a window, not a remnant"


def test_what_survives_is_the_newest(run):
    """Eviction is by arrival order and never by content, so what is kept is
    always the part nearest whatever just happened."""
    reported = _reported(run)
    assert reported["LAST"] == f"row {_WRITTEN - 1} processed"
    assert reported["FIRST"] != "row 0 processed"
