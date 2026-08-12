"""Subprocess-based integration tests.

sys.excepthook and atexit can't be exercised meaningfully in-process (pytest
owns exception handling; atexit only runs at real interpreter shutdown), so
these run the standalone scripts under tests/scripts/ as real child
processes and inspect their stderr/exit code/on-disk store afterward.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


def _run_script(
    scripts_dir: Path, name: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    full_env = dict(os.environ)
    src_dir = str(Path(__file__).parent.parent / "src")
    full_env["PYTHONPATH"] = os.pathsep.join([src_dir, full_env.get("PYTHONPATH", "")])
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(scripts_dir / name)],
        capture_output=True,
        env=full_env,
        timeout=30,
    )


def test_traceback_intact_after_raise_in_plain_mode(scripts_dir):
    result = _run_script(scripts_dir, "raise_after_init.py")
    assert result.returncode == 1
    stderr = result.stderr
    assert b"Traceback (most recent call last):" in stderr
    assert b"RuntimeError: boom" in stderr
    tb_start = stderr.index(b"Traceback (most recent call last):")
    tb_block = stderr[tb_start:]
    assert not _ANSI_RE.search(tb_block)


def test_traceback_intact_after_raise_in_rich_mode(scripts_dir):
    pytest.importorskip("rich")
    result = _run_script(scripts_dir, "raise_after_init_rich.py")
    assert result.returncode == 1
    stderr = result.stderr
    assert b"Traceback (most recent call last):" in stderr
    assert b"RuntimeError: boom" in stderr
    tb_start = stderr.index(b"Traceback (most recent call last):")
    tb_block = stderr[tb_start:]
    assert not _ANSI_RE.search(tb_block)


def test_piped_output_has_no_ansi_in_plain_mode(scripts_dir):
    result = _run_script(scripts_dir, "raise_after_init.py")
    assert not _ANSI_RE.search(result.stderr)


def test_write_through_records_printed_once_on_clean_exit(scripts_dir, tmp_path):
    # Regression: the atexit diagnostic dump replayed the buffer unconditionally,
    # so a write-through renderer printed the entire run a second time.
    result = _run_script(
        scripts_dir,
        "log_then_exit.py",
        env={
            "LUMBERJACK_TEST_DB_PATH": str(tmp_path / "records.db"),
            "LUMBERJACK_TEST_RECORD_COUNT": "5",
        },
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    for i in range(5):
        assert result.stderr.count(f"record {i}".encode()) == 1, result.stderr


def test_write_through_records_printed_once_after_traceback(scripts_dir):
    result = _run_script(scripts_dir, "raise_after_init.py")
    assert result.stderr.count(b"about to fail") == 1, result.stderr


def test_no_records_lost_on_process_exit(scripts_dir, tmp_path):
    db_path = tmp_path / "records.db"
    result = _run_script(
        scripts_dir,
        "log_then_exit.py",
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
