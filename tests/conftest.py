"""Shared pytest fixtures."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _reset_lumberjack_state() -> Iterator[None]:
    root = logging.getLogger()
    prev_handlers = root.handlers[:]
    prev_level = root.level
    yield
    import lumberjack

    if lumberjack._installed:
        lumberjack.shutdown()
    root.handlers[:] = prev_handlers
    root.setLevel(prev_level)
