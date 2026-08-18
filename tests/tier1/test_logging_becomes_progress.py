"""Tier 1: a loop that logs becomes a bar, in a real process.

The premise the package exists for, asserted from outside: a child process
runs `tests/tier1/scripts/loop_to_bar.py`, and this reads only its stdout,
stderr and exit code. Nothing here imports lumberjack — see
`tests/README.md` for why that rule is the one holding the tier together.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - drives a real child process
from pathlib import Path

import pytest

from subprocess_rig import ANSI_RE, child_env, needs_rich, run_script

_SCRIPTS = Path(__file__).parent / "scripts"


@pytest.fixture(scope="module")
def loop_run() -> subprocess.CompletedProcess[bytes]:
    """One launch, read by every test below.

    Module-scoped for the reason `test_exit_paths.py`'s `raise_after_init_plain`
    is: these assert disjoint things about one program's output, and relaunching
    it per test buys seconds of subprocess time and nothing else. Module scope
    is why the environment comes from `child_env()` rather than the
    function-scoped `subprocess_env` fixture.

    `COLUMNS` is set because the assertions read a rendered frame, and a
    narrow terminal truncates the very columns they look for.
    """
    return run_script(_SCRIPTS, "loop_to_bar.py", child_env, env={"COLUMNS": "200"})


def test_the_script_finishes_cleanly(loop_run):
    assert loop_run.returncode == 0, loop_run.stderr.decode(errors="replace")


@needs_rich
def test_two_hundred_log_lines_become_one_rendered_line(loop_run):
    """The MVP. 200 lines in, no scrolling out, one bar for the loop."""
    stderr = loop_run.stderr.decode("utf-8", errors="replace")
    # Not "never appears": the session heartbeat echoes the newest line beside
    # its arrival count, which is a row rewritten in place rather than 200
    # lines scrolling past. So the premise is that exactly one *rendered* line
    # is on screen, and it is the last.
    assert re.findall(r"processing item \d+", stderr) == ["processing item 199"], stderr


@needs_rich
def test_the_bar_is_named_by_the_message_template(loop_run):
    """`record.msg` — the template stdlib kept separate from the data, so no
    rendered text is ever parsed to name a row."""
    stderr = loop_run.stderr.decode("utf-8", errors="replace")
    assert "processing item …" in stderr, stderr


@needs_rich
def test_the_count_is_iterations_not_records(loop_run):
    """The display unit is the loop, not the call site."""
    stderr = loop_run.stderr.decode("utf-8", errors="replace")
    assert "200 iterations" in stderr, stderr


def test_the_store_kept_every_record(loop_run):
    """Lossy display, lossless store: 200 loop records plus the warning.

    Unmarked, so the `bare install (no rich)` job runs it too — losslessness
    is a property of capture and owes nothing to the renderer.
    """
    stdout = loop_run.stdout.decode("utf-8", errors="replace")
    assert "STORED=201" in stdout, stdout


def test_a_warning_survives_the_collapse(loop_run):
    """The one line a user is guaranteed to see must not be swallowed, and no
    cursor control may reach a consumer that is not a terminal.

    Deliberately unmarked. Rich is requested and stderr is a pipe, so on a
    bare install this measures the plain renderer instead — which is the
    Principle 9 degradation check, and it is lost the moment this is gated.
    """
    assert loop_run.stderr.count(b"something looked odd") == 1, loop_run.stderr
    assert not ANSI_RE.search(loop_run.stderr), loop_run.stderr
