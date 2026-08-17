"""Subprocess-based exit-path tests: excepthook, atexit, and the on-disk store.

sys.excepthook and atexit can't be exercised meaningfully in-process (pytest
owns exception handling; atexit only runs at real interpreter shutdown), so
these run the standalone scripts under tests/scripts/ as real child
processes and inspect their stderr/exit code/on-disk store afterward.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# The `?` matters: these are *negative* assertions, and without it a
# private-mode sequence such as hide-cursor (`\x1b[?25l`) slips through
# unmatched — the exact drift the conftest `strip_ansi` note records.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")

#: Some of these scripts ask for `output_mode="rich"` and then assert on what
#: only the *lossy* live bar does — collapsing the loop, and the exit dump that
#: recovers it. On a bare install the factory hands back the write-through
#: plain renderer instead, which correctly prints everything and dumps nothing,
#: so the assertions below would be measuring the fallback, not the bar.
_needs_rich = pytest.mark.skipif(
    importlib.util.find_spec("rich") is None,
    reason="asserts live-bar behaviour, which degrades to plain without rich",
)


def _run_script(
    scripts_dir: Path,
    name: str,
    subprocess_env: Callable[..., dict[str, str]],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(scripts_dir / name)],
        capture_output=True,
        env=subprocess_env(env),
        timeout=30,
    )


@pytest.fixture(scope="module")
def raise_after_init_plain() -> subprocess.CompletedProcess[bytes]:
    """`raise_after_init.py`, run once and shared by every test that reads
    its result — three tests used to launch it separately for three
    disjoint assertion sets on the same output.

    Module-scoped, so it cannot request the function-scoped `subprocess_env`
    fixture (`_run_script`'s other callers do); the environment is built
    inline here instead, deliberately duplicating those few lines rather than
    forcing this fixture down to function scope and re-launching the process
    per test.
    """
    scripts_dir = Path(__file__).parent / "scripts"
    full_env = dict(os.environ)
    src_dir = str(Path(__file__).parent.parent / "src")
    full_env["PYTHONPATH"] = os.pathsep.join([src_dir, full_env.get("PYTHONPATH", "")])
    full_env["PYTHONIOENCODING"] = "utf-8"
    full_env["COVERAGE_PROCESS_START"] = str(
        Path(__file__).parent.parent / "pyproject.toml"
    )
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(scripts_dir / "raise_after_init.py")],
        capture_output=True,
        env=full_env,
        timeout=30,
    )


def test_traceback_intact_after_raise_in_plain_mode(raise_after_init_plain):
    result = raise_after_init_plain
    assert result.returncode == 1
    stderr = result.stderr
    assert b"Traceback (most recent call last):" in stderr
    assert b"RuntimeError: boom" in stderr
    tb_start = stderr.index(b"Traceback (most recent call last):")
    tb_block = stderr[tb_start:]
    assert not _ANSI_RE.search(tb_block)
    # Superset of the old test_piped_output_has_no_ansi_in_plain_mode: no
    # cursor control anywhere in the stream, not only inside the traceback.
    assert not _ANSI_RE.search(stderr)


def test_traceback_intact_after_raise_in_rich_mode(scripts_dir, subprocess_env):
    pytest.importorskip("rich")
    result = _run_script(scripts_dir, "raise_after_init_rich.py", subprocess_env)
    assert result.returncode == 1
    stderr = result.stderr
    assert b"Traceback (most recent call last):" in stderr
    assert b"RuntimeError: boom" in stderr
    tb_start = stderr.index(b"Traceback (most recent call last):")
    tb_block = stderr[tb_start:]
    assert not _ANSI_RE.search(tb_block)


def test_write_through_records_printed_once_on_clean_exit(
    scripts_dir, subprocess_env, tmp_path
):
    # Regression: the atexit diagnostic dump replayed the buffer unconditionally,
    # so a write-through renderer printed the entire run a second time.
    result = _run_script(
        scripts_dir,
        "log_then_exit.py",
        subprocess_env,
        env={
            "LUMBERJACK_TEST_DB_PATH": str(tmp_path / "records.db"),
            "LUMBERJACK_TEST_RECORD_COUNT": "5",
        },
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    for i in range(5):
        assert result.stderr.count(f"record {i}".encode()) == 1, result.stderr


def test_write_through_records_printed_once_after_traceback(raise_after_init_plain):
    assert (
        raise_after_init_plain.stderr.count(b"about to fail") == 1
    ), raise_after_init_plain.stderr


def _store_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()


@_needs_rich
def test_a_logging_loop_becomes_a_bar_in_a_real_process(
    scripts_dir, subprocess_env, tmp_path
):
    # The Phase 1 premise, end to end in its own interpreter: 200 log lines in,
    # no scrolling out, one bar for the loop that produced them.
    db_path = tmp_path / "records.db"
    result = _run_script(
        scripts_dir,
        "loop_then_exit.py",
        subprocess_env,
        env={
            "LUMBERJACK_TEST_DB_PATH": str(db_path),
            "LUMBERJACK_TEST_RECORD_COUNT": "200",
            "COLUMNS": "200",
        },
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    # Not "never appears": the session heartbeat echoes the newest line beside
    # its arrival count (#54), which is a row rewritten in place rather than
    # 200 lines scrolling past. So the premise is that exactly one *rendered*
    # line is on screen, and it is the last. The bar's own label is the
    # message template, which is a different string and not one of the 200.
    rendered = re.findall(r"processing item \d+", stderr)
    assert rendered == ["processing item 199"], stderr
    # The label is `record.msg` with its format specifiers substituted — the
    # template stdlib kept separate from the data, needing no parsing of
    # rendered text (#56).
    assert "processing item …" in stderr, stderr
    # Iterations of the loop, not records captured; the store below is where
    # the record count is asked for.
    assert "200 iterations" in stderr, stderr
    # Lossy display, lossless store.
    assert _store_count(db_path) == 201  # 200 loop records + the warning


def test_a_warning_survives_the_collapse(scripts_dir, subprocess_env, tmp_path):
    """A rich bar is explicitly requested but stderr is a pipe, so the
    warning inside the collapsed loop must still reach it exactly once — and
    no cursor control may reach a consumer that isn't a terminal."""
    result = _run_script(
        scripts_dir,
        "loop_then_exit.py",
        subprocess_env,
        env={
            "LUMBERJACK_TEST_DB_PATH": str(tmp_path / "records.db"),
            "LUMBERJACK_TEST_RECORD_COUNT": "50",
        },
    )
    assert result.stderr.count(b"something looked odd") == 1, result.stderr
    assert not _ANSI_RE.search(result.stderr), result.stderr


@_needs_rich
@pytest.mark.parametrize(
    "extra_env",
    [
        pytest.param({}, id="pump and final flush left on"),
        pytest.param(
            {
                "LUMBERJACK_TEST_FLUSH_INTERVAL": "0",
                "LUMBERJACK_TEST_FINAL_FLUSH": "0",
            },
            id="pump and final flush both off",
        ),
    ],
)
def test_lossy_renderer_dumps_the_tail_at_exit(
    scripts_dir, subprocess_env, tmp_path, extra_env
):
    """The other half of the write_through=False contract: records the bar
    swallowed are replayed at exit, and still reach the store — whether the
    pump drained the buffer long before exit (the regression: the dump used
    to read the handler's buffer, which is empty by then) or the whole run
    was still sitting in the buffer because the pump and the final flush were
    both disabled (the ordering guard: the dump reads the store, so the
    atexit drain has to run first or it dumps nothing)."""
    db_path = tmp_path / "records.db"
    result = _run_script(
        scripts_dir,
        "loop_then_exit.py",
        subprocess_env,
        env={
            "LUMBERJACK_TEST_DB_PATH": str(db_path),
            "LUMBERJACK_TEST_RECORD_COUNT": "20",
            "LUMBERJACK_TEST_DUMP_LAST_N": "5",
            **extra_env,
        },
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"processing item 19" in result.stderr, result.stderr
    assert b"processing item 0" not in result.stderr, "dumped more than the tail"
    assert _store_count(db_path) == 21


def test_no_records_lost_on_process_exit(scripts_dir, subprocess_env, tmp_path):
    db_path = tmp_path / "records.db"
    result = _run_script(
        scripts_dir,
        "log_then_exit.py",
        subprocess_env,
        env={
            "LUMBERJACK_TEST_DB_PATH": str(db_path),
            "LUMBERJACK_TEST_RECORD_COUNT": "25",
        },
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()
    assert count == 25
