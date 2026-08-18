"""lumberjack: a drop-in UX layer for stdlib logging.

`init()` is for applications only — it takes exclusive ownership of the root
logger's handlers. Libraries should never call it; the tracking API (`task`)
works whether or not `init()` has run, and is inert without it.
"""

from __future__ import annotations

import importlib.metadata
import logging

from lumberjack import session as _registry
from lumberjack import teardown
from lumberjack.detect import OutputMode, OutputModeDetector
from lumberjack.handler import DEFAULT_BUFFER_SIZE, LumberjackHandler
from lumberjack.pump import DEFAULT_FLUSH_INTERVAL, FlushPump
from lumberjack.renderers import Renderer, create_renderer
from lumberjack.session import Session
from lumberjack.store import RecordStore, SQLiteRecordStore
from lumberjack.tracking import TaskHandle, task, track

try:
    __version__ = importlib.metadata.version("lumberjack")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "TaskHandle",
    "__version__",
    "current_handler",
    "current_output_mode",
    "current_renderer",
    "current_store",
    "flush",
    "init",
    "is_initialized",
    "shutdown",
    "task",
    "track",
]


def init(
    *,
    level: int = logging.DEBUG,
    output_mode: OutputMode | str | None = None,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    store: RecordStore | None = None,
    replace_handlers: bool = True,
    dump_last_n: int = 50,
    flush_interval: float = DEFAULT_FLUSH_INTERVAL,
) -> LumberjackHandler:
    """Install lumberjack on the root logger.

    `level` sets both the root logger's level and the handler's, and defaults
    to DEBUG rather than stdlib's usual WARNING or a tidier INFO. That is the
    whole point: `logger.debug(...)` calls inside loops are what lumberjack
    turns into progress, and any higher default has stdlib discard them before
    lumberjack ever sees them — a zero-config first run that shows nothing.
    The volume is handled where it should be, by the display collapsing it and
    the store absorbing it. Pass `level=logging.INFO` for a quieter capture.

    Replaces the root logger's existing handlers by default; pass
    `replace_handlers=False` to layer alongside them instead. Raises
    RuntimeError if already installed — call `shutdown()` first.

    `flush_interval` is how often (seconds) the write buffer is drained into
    the store; pass 0 to disable the pump and drain only on `flush()` and at
    exit. A `store` passed in here belongs to the caller and is left open by
    `shutdown()`; one lumberjack creates itself is closed.

    Failing partway through leaves the process as it was found: a store
    created here is closed again rather than left open and unreachable.
    """
    # The whole body, because the "already installed?" check and the publish
    # that satisfies it are far apart: two threads racing here would both pass
    # the check and the second would silently orphan the first's handler.
    with _registry.registry_lock():
        return _init_locked(
            level=level,
            output_mode=output_mode,
            buffer_size=buffer_size,
            store=store,
            replace_handlers=replace_handlers,
            dump_last_n=dump_last_n,
            flush_interval=flush_interval,
        )


def _init_locked(
    *,
    level: int,
    output_mode: OutputMode | str | None,
    buffer_size: int,
    store: RecordStore | None,
    replace_handlers: bool,
    dump_last_n: int,
    flush_interval: float,
) -> LumberjackHandler:
    """`init()`'s body, with `registry_lock()` already held."""
    if _registry.current_session() is not None:
        raise RuntimeError(
            "lumberjack.init() already called; call lumberjack.shutdown() first"
        )

    owns_store = store is None
    resolved_store = store if store is not None else SQLiteRecordStore(":memory:")
    try:
        mode = OutputModeDetector(override=output_mode).detect()
        # The renderer gets the store, not just the record stream: a live bar
        # reads its counts back out of the store (store, then render).
        renderer = create_renderer(mode, store=resolved_store)

        handler = LumberjackHandler(
            buffer_size=buffer_size,
            on_record=renderer.render,
            level=level,
        )
        root = logging.getLogger()
        session = Session(
            handler=handler,
            store=resolved_store,
            renderer=renderer,
            output_mode=mode,
            owns_store=owns_store,
            dump_last_n=dump_last_n,
            prev_handlers=root.handlers[:] if replace_handlers else [],
            prev_level=root.level,
        )
        # Inside the guard rather than after it: `install()` raises when
        # teardown is already installed, so it is one more thing that can fail
        # while a store this call created is still open. From 3.13 an unclosed
        # sqlite connection also emits a ResourceWarning at collection, which
        # this suite turns into an error in whichever unrelated test happens
        # to trigger it.
        teardown.install(session)
    except BaseException:
        if owns_store:
            resolved_store.close()
        raise

    # Everything that can fail is done; the root logger is only touched once
    # the install is guaranteed to complete, so there is no half-swapped state
    # to unwind here.
    for existing in session.prev_handlers:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    _registry.set_current_session(session)

    if flush_interval > 0:
        session.pump = FlushPump(interval=flush_interval, flush=flush)
        session.pump.start()

    return handler


def shutdown() -> None:
    """Tear down lumberjack: stop the pump, flush, restore the root logger.

    Restores both halves of what `init()` took over — the handler list *and*
    the level — so a library that only wanted lumberjack for part of a run
    does not silently leave the root logger more verbose than it found it.

    Closes the store only if lumberjack created it — a store the caller passed
    to `init()` stays open so it can still be queried afterward.
    """
    session = _registry.current_session()
    if session is None:
        return
    # Before the lock, and it has to be: `stop()` joins the pump thread, and
    # that thread spends its life calling `flush()`, which takes this lock.
    # Joining it from inside the lock deadlocks on the first tick that
    # overlaps. Once `stop()` returns the pump is dead and cannot re-enter.
    if session.pump is not None:
        session.pump.stop()
    with _registry.registry_lock():
        session = _registry.current_session()
        if session is None:
            # Another thread shut down while this one was stopping the pump.
            return
        flush()
        # A live display owns a timer thread and the terminal; leaving it
        # running past shutdown() would leak both.
        session.renderer.close()
        teardown.uninstall()
        root = logging.getLogger()
        root.removeHandler(session.handler)
        for h in session.prev_handlers:
            root.addHandler(h)
        root.setLevel(session.prev_level)
        if session.owns_store:
            session.store.close()
        _registry.set_current_session(None)


def flush() -> None:
    """Drain the handler's buffer into the store on demand.

    Holds `registry_lock()` across the read and the write. Without it a
    concurrent `shutdown()` can close the store between them, and the append
    raises `sqlite3.ProgrammingError` into whichever thread called this —
    which for the pump is lumberjack's own, but for anyone calling `flush()`
    by hand is theirs.

    `teardown._flush_buffer` is a deliberate lock-free copy of the
    drain-then-append below (see the comment there for why); a change to the
    protocol here has to land in both places.
    """
    with _registry.registry_lock():
        session = _registry.current_session()
        if session is None:
            return
        rows = session.handler.drain()
        if rows:
            session.store.append(rows)


def is_initialized() -> bool:
    """True between a successful `init()` and the matching `shutdown()`."""
    return _registry.current_session() is not None


def current_handler() -> LumberjackHandler | None:
    """The installed handler, or None if `init()` hasn't run."""
    session = _registry.current_session()
    return session.handler if session is not None else None


def current_store() -> RecordStore | None:
    """The store records are being written to, or None if `init()` hasn't run.

    The supported way to query captured records. It is `RecordStore | None`,
    so a type checker will make a caller say why it knows better::

        store = lumberjack.current_store()
        assert store is not None  # init() ran
        rows = store.recent(n=100)
    """
    session = _registry.current_session()
    return session.store if session is not None else None


def current_renderer() -> Renderer | None:
    """The renderer chosen for the detected output mode, or None."""
    session = _registry.current_session()
    return session.renderer if session is not None else None


def current_output_mode() -> OutputMode | None:
    """The output mode `init()` resolved to, after override/env/TTY detection."""
    session = _registry.current_session()
    return session.output_mode if session is not None else None
