"""Shared pytest fixtures."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from lumberjack.schema import LogRecordRow
from lumberjack.store import RecordStore, SQLiteRecordStore


def _available_backends() -> dict[str, Callable[[], RecordStore]]:
    backends: dict[str, Callable[[], RecordStore]] = {
        "sqlite": lambda: SQLiteRecordStore(":memory:"),
    }
    try:
        import duckdb  # noqa: F401
    except ImportError:
        pass
    # else: a future DuckDBRecordStore slots in here, e.g.:
    #     from lumberjack.store import DuckDBRecordStore
    #     backends["duckdb"] = lambda: DuckDBRecordStore(":memory:")
    return backends


@pytest.fixture(params=list(_available_backends()), ids=lambda k: k)
def store(request: pytest.FixtureRequest) -> Iterator[RecordStore]:
    backend = _available_backends()[request.param]()
    yield backend
    backend.close()


@pytest.fixture
def scripts_dir() -> Path:
    return Path(__file__).parent / "scripts"


@pytest.fixture
def make_row() -> Callable[..., LogRecordRow]:
    """Build a LogRecordRow with sane defaults; override any field by keyword."""

    def _make(**overrides: object) -> LogRecordRow:
        fields: dict[str, object] = dict(
            logger_name="test",
            level_name="INFO",
            level_no=20,
            msg="msg",
            message="hello world",
            pathname="/tmp/foo.py",
            filename="foo.py",
            module="foo",
            func_name="bar",
            lineno=10,
            created=time.time(),
            thread=1,
            thread_name="MainThread",
            process=100,
            process_name="MainProcess",
            exc_text=None,
            stack_text=None,
            asyncio_task_name=None,
            asyncio_task_id=None,
            task_id=None,
            parent_task_id=None,
            task_label=None,
            task_event=None,
            progress_current=None,
            progress_total=None,
            template_id=None,
        )
        fields.update(overrides)
        return LogRecordRow(**fields)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def wait_until() -> Callable[..., bool]:
    """Poll `predicate` until true or `timeout` elapses; returns whether it held.

    Returns as soon as the predicate passes, so a healthy assertion costs
    milliseconds — only a genuine failure waits out the full timeout.
    """

    def _wait(
        predicate: Callable[[], bool],
        *,
        timeout: float = 5.0,
        interval: float = 0.005,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    return _wait


@pytest.fixture(autouse=True)
def _reset_lumberjack_state() -> Iterator[None]:
    root = logging.getLogger()
    prev_handlers = root.handlers[:]
    yield
    import lumberjack

    if lumberjack.is_initialized():
        lumberjack.shutdown()
    # Load-bearing, unlike a level restore would be: `shutdown()` puts back
    # the handlers *it* replaced, but tests that add one to the root logger
    # and never init — or that init and fail — leave it behind otherwise.
    root.handlers[:] = prev_handlers
