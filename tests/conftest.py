"""Shared pytest fixtures.

Everything here earned its place by being written twice: each fixture below
replaced two or more per-file copies (some byte-identical, docstrings
included), and the copies are what drifted — two ANSI strippers disagreed
about cursor-control sequences, and two period-estimation helpers disagreed
about a watermark guard. One definition is the fix, not tidiness.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import re
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import lumberjack
from lumberjack import static, tracking
from lumberjack.schema import LogRecordRow, StoredRecord, TaskEvent
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
def make_log_record() -> Callable[..., logging.LogRecord]:
    """Build a raw stdlib `LogRecord` with sane defaults; override by keyword.

    The input side of `make_row`'s output: what `LogRecordRow.from_log_record`
    is handed, for the tests that exercise that conversion rather than
    starting from a row. `pathname` matches `make_row`'s default so the two
    describe the same fictional source location from either end.

    Not `__file__`, which is what both per-file copies of this said before
    they were folded into one. That was correct while it sat in the test
    module and silently wrong here: it would resolve to *conftest*, so
    `pathname`, `filename` and `module` — the three columns source-location
    identity is built on — would report this file rather than the caller's.
    Nothing asserts on them today, which is exactly why it would have gone
    unnoticed.
    """

    def _make(**overrides: object) -> logging.LogRecord:
        kwargs: dict[str, object] = dict(
            name="test.logger",
            level=logging.INFO,
            pathname="/nonexistent/foo.py",
            lineno=42,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        kwargs.update(overrides)
        return logging.LogRecord(**kwargs)  # type: ignore[arg-type]

    return _make


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
            pathname="/nonexistent/foo.py",
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
def _fresh_static_cache() -> Iterator[None]:
    """Files written per test land at paths the parse cache has never seen,
    but the cache is module state: a stale entry from a previous test's
    `tmp_path`, or a test that edits a path another test parsed, would
    otherwise be read back. Autouse for the whole suite because clearing a
    dict is free and the alternative — each display/static/lint file carrying
    its own copy of this fixture — is how one file (test_render_progress)
    ended up writing modules with no cache guard at all."""
    static.clear_cache()
    yield
    static.clear_cache()


@pytest.fixture
def write_module(tmp_path: Path) -> Callable[..., Path]:
    """Write dedented source to a uniquely named module and return its path.

    `strip=False` keeps a leading blank line (and therefore the line numbers)
    exactly as the triple-quoted literal states them — the display tests
    assert linenos against the file, so for them a strip would shift every
    assertion by one.
    """
    counter = itertools.count()

    def _write(source: str, *, name: str | None = None, strip: bool = True) -> Path:
        text = textwrap.dedent(source)
        if strip:
            text = text.lstrip()
        path = tmp_path / (name or f"mod{next(counter)}.py")
        path.write_text(text, encoding="utf-8")
        return path

    return _write


#: One pattern for every ANSI escape the display writes, cursor control
#: included: the `?` admits private-mode sequences such as `\x1b[?25l`
#: (hide cursor), which a `[0-9;]`-only pattern silently leaves in place.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@pytest.fixture
def strip_ansi() -> Callable[[str], str]:
    return lambda text: _ANSI_RE.sub("", text)


@pytest.fixture
def loop_rows(
    make_row: Callable[..., LogRecordRow],
) -> Callable[..., list[LogRecordRow]]:
    """N records from one source location, the way a loop emits them."""

    def _rows(n: int, **overrides: object) -> list[LogRecordRow]:
        return [make_row(message=f"item {i}", **overrides) for i in range(n)]

    return _rows


@pytest.fixture
def make_task_event() -> Callable[..., TaskEvent]:
    """A TaskEvent with every progress column populated; override by keyword."""

    def _make(**overrides: object) -> TaskEvent:
        fields: dict[str, object] = dict(
            label="reindex",
            kind="update",
            task_id=7,
            parent_task_id=3,
            current=40,
            total=100,
        )
        fields.update(overrides)
        return TaskEvent(**fields)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def make_task_row(
    make_row: Callable[..., LogRecordRow],
) -> Callable[..., LogRecordRow]:
    """A stored row of the shape the tracking API writes."""

    def _make(task_id: int, event: str, **kw: object) -> LogRecordRow:
        return make_row(
            task_id=task_id,
            task_event=event,
            task_label=kw.pop("label", "job"),
            **kw,
        )

    return _make


class TrackingSession:
    """A live `init()` plus a way to read back what it stored.

    The pump is off and `read()` flushes explicitly, so nothing here waits on
    a timer. The store is caller-supplied, so `shutdown()` leaves it open.
    """

    def __init__(self, store: SQLiteRecordStore) -> None:
        self._store = store

    def read(self) -> list[StoredRecord]:
        lumberjack.flush()
        return [r for r in self._store.recent() if r.task_event]

    def events(self) -> list[tuple[str | None, str | None]]:
        return [(r.task_event, r.task_label) for r in self.read()]


@pytest.fixture
def make_session() -> Callable[..., contextlib.AbstractContextManager[TrackingSession]]:
    """A factory over the `session` fixture, for tests that need init() kwargs
    the plain fixture does not take (`replace_handlers=False`, a level)."""

    @contextlib.contextmanager
    def _make(**init_kwargs: object) -> Iterator[TrackingSession]:
        store = SQLiteRecordStore(":memory:")
        kwargs: dict[str, object] = dict(output_mode="plain", flush_interval=0)
        kwargs.update(init_kwargs)
        lumberjack.init(store=store, **kwargs)  # type: ignore[arg-type]
        try:
            yield TrackingSession(store)
        finally:
            lumberjack.shutdown()
            store.close()

    return _make


@pytest.fixture
def session(
    make_session: Callable[..., contextlib.AbstractContextManager[TrackingSession]],
) -> Iterator[TrackingSession]:
    with make_session() as live:
        yield live


@pytest.fixture
def attached_logger() -> (
    Callable[..., contextlib.AbstractContextManager[logging.Logger]]
):
    """A non-propagating DEBUG logger with `handler` attached, detached after."""

    @contextlib.contextmanager
    def _attach(
        handler: logging.Handler, name: str = "attached-logger-test"
    ) -> Iterator[logging.Logger]:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            yield logger
        finally:
            logger.removeHandler(handler)

    return _attach


@pytest.fixture
def root_sentinel() -> Iterator[logging.Handler]:
    """A NullHandler planted on the root logger, to observe what `init()` and
    `shutdown()` do to handlers that were there first. Removed afterwards
    whether or not the test's init/shutdown put it back."""
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        yield sentinel
    finally:
        root.removeHandler(sentinel)


@pytest.fixture
def subprocess_env() -> Callable[..., dict[str, str]]:
    """The environment a child process needs to exercise lumberjack from src.

    One place instead of three, because every copy had to restate the same
    two non-obvious lines: COVERAGE_PROCESS_START makes pytest-cov's .pth
    hook measure the child (the excepthook/atexit/file-store paths run only
    there), and PYTHONIOENCODING pins the child's stdio so the display's `…`
    and `━` survive a Windows default of cp1252 — what the display does on a
    terminal that cannot encode them is a separate question with its own
    tests.
    """

    def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
        full_env = dict(os.environ)
        src_dir = str(Path(__file__).parent.parent / "src")
        full_env["PYTHONPATH"] = os.pathsep.join(
            [src_dir, full_env.get("PYTHONPATH", "")]
        )
        full_env["PYTHONIOENCODING"] = "utf-8"
        full_env["COVERAGE_PROCESS_START"] = str(
            Path(__file__).parent.parent / "pyproject.toml"
        )
        if extra:
            full_env.update(extra)
        return full_env

    return _env


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
    # A test that leaves a task handle entered would otherwise make the next
    # test's tasks children of a dead one.
    tracking._current_task.set(None)
