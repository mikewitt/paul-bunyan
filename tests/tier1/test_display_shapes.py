"""Tier 1: what the display does with each shape of log stream.

One script per shape, each the smallest program that produces it. Together
they pin the two claims the display rests on — that a row counts the work,
not the logging, and that a source which never repeats is not called a loop.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - drives real child processes
from pathlib import Path

import pytest

from script_runner import ANSI_RE, child_env, needs_rich, run_script

_SCRIPTS = Path(__file__).parent / "scripts"


def _frame(result: subprocess.CompletedProcess[bytes]) -> list[str]:
    """The child's stderr as rendered lines, ANSI stripped and blanks dropped."""
    text = ANSI_RE.sub(b"", result.stderr).decode("utf-8", errors="replace")
    return [line for line in re.split(r"[\r\n]+", text) if line.strip()]


@pytest.fixture(scope="module")
def siblings_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "siblings_merge.py", child_env, env={"COLUMNS": "200"})


@pytest.fixture(scope="module")
def oneshot_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "oneshot_startup.py", child_env, env={"COLUMNS": "200"})


@pytest.fixture(scope="module")
def piped_run() -> subprocess.CompletedProcess[bytes]:
    return run_script(_SCRIPTS, "piped_is_plain.py", child_env)


@needs_rich
def test_four_call_sites_in_one_body_draw_one_row(siblings_run):
    """The display unit is the loop, not the call site."""
    rows = [line for line in _frame(siblings_run) if "iterations" in line]
    assert len(rows) == 1, rows


@needs_rich
def test_the_row_counts_iterations_not_records(siblings_run):
    """100 rows processed, 400 log calls made. The domain object is the row.

    `400` is *not* absent from the frame and should not be: the heartbeat
    reports 400 events, which is the honest identity-layer number. What must
    not appear is 400 presented as iterations of the loop.
    """
    frame = "\n".join(_frame(siblings_run))
    assert "100 iterations" in frame, frame
    assert "400 iterations" not in frame, frame


def test_every_record_reaches_the_store(siblings_run):
    """The count the display collapsed is still the count the store kept."""
    assert b"STORED=400" in siblings_run.stdout, siblings_run.stdout


@needs_rich
def test_sources_that_never_repeat_draw_no_loop_row(oneshot_run):
    """Six startup lines, each firing once. No source has a period, so no
    loop row can honestly be drawn — and the display says so by not drawing
    one, rather than by calling a single record an iteration.

    The heartbeat may still show the newest line and an arrival count; that
    is liveness, not progress, so the assertion is about the *row*.
    """
    assert oneshot_run.returncode == 0, oneshot_run.stderr
    rows = [line for line in _frame(oneshot_run) if "iterations" in line]
    assert rows == [], rows


#: What the plain renderer writes: a timestamp, a padded level, the logger
#: name, and the message. Matching the whole line rather than the message
#: inside it is the point — `re.findall(rb"processing item \d+")` counted 20
#: under `LUMBERJACK_OUTPUT_MODE=json` too, so the pair asserted "not rich"
#: while claiming to assert "plain".
_PLAIN_LINE = re.compile(rb"^\S+ INFO\s+ingest - processing item (\d+)$")


def test_a_pipe_gets_write_through_text(piped_run):
    """Principle 5: never assume a human is watching. With no `output_mode`
    given and stderr a pipe, detection picks plain — every record printed in
    order, nothing redrawn in place.

    All three of those are asserted: the *format* is plain rather than JSON
    lines, every record is present, and they are in the order they were
    logged. Equality on the sequence, as `test_logging_becomes_progress.py`
    does for the other switch.

    Unmarked: this is what the *absence* of rich looks like too, so it holds
    on the bare install and is a degradation check there.
    """
    assert piped_run.returncode == 0, piped_run.stderr
    numbered = [
        int(match.group(1))
        for line in piped_run.stderr.splitlines()
        if (match := _PLAIN_LINE.match(line))
    ]
    assert numbered == list(range(20)), piped_run.stderr


def test_a_pipe_gets_no_cursor_control(piped_run):
    assert not ANSI_RE.search(piped_run.stderr), piped_run.stderr
