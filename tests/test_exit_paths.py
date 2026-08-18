"""Subprocess-based exit-path tests: excepthook, atexit, and the on-disk store.

sys.excepthook and atexit can't be exercised meaningfully in-process (pytest
owns exception handling; atexit only runs at real interpreter shutdown), so
these run the standalone scripts under tests/scripts/ as real child
processes and inspect their stderr/exit code/on-disk store afterward.

The premise tests that used to live here — a loop becoming a bar, a warning
surviving the collapse — moved to `tests/tier1/`. They asserted the product,
not an exit path, and were here only because the subprocess machinery was.
"""

from __future__ import annotations

import sqlite3
import subprocess  # nosec B404 - these tests launch real child processes
import sys
from pathlib import Path

import pytest

from script_runner import ANSI_RE as _ANSI_RE
from script_runner import child_env
from script_runner import needs_rich as _needs_rich
from script_runner import run_script as _run_script


@pytest.fixture(scope="module")
def raise_after_init_plain() -> subprocess.CompletedProcess[bytes]:
    """`raise_after_init.py`, run once and shared by every test that reads
    its result — three tests used to launch it separately for three
    disjoint assertion sets on the same output.

    Module-scoped, so it cannot request the function-scoped `subprocess_env`
    fixture; it calls `child_env()` directly instead, which is why that is a
    plain function and not only a fixture.
    """
    return subprocess.run(  # noqa: S603  # nosec B603
        [
            sys.executable,
            str(Path(__file__).parent / "scripts" / "raise_after_init.py"),
        ],
        capture_output=True,
        env=child_env(),
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
