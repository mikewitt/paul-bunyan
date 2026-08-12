"""lumberjack: a drop-in UX layer for stdlib logging.

`init()` is for applications only — it takes exclusive ownership of the root
logger's handlers. Libraries should never call it; the future tracking API
(`track`/`task`) is designed to work whether or not `init()` has run.
"""

from __future__ import annotations

import importlib.metadata
import logging

from lumberjack import teardown
from lumberjack.detect import OutputMode, OutputModeDetector
from lumberjack.handler import DEFAULT_BUFFER_SIZE, LumberjackHandler
from lumberjack.pump import DEFAULT_FLUSH_INTERVAL, FlushPump
from lumberjack.renderers import Renderer, create_renderer
from lumberjack.session import Session
from lumberjack.store import RecordStore, SQLiteRecordStore

try:
    __version__ = importlib.metadata.version("lumberjack")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "init",
    "shutdown",
    "flush",
    "is_initialized",
    "current_handler",
    "current_store",
    "current_renderer",
    "current_output_mode",
    "__version__",
]

# The one piece of module state, and it is unguarded by any lock.
# lumberjack: see issue #11
_session: Session | None = None


def init(
    *,
    level: int = logging.INFO,
    output_mode: OutputMode | str | None = None,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    store: RecordStore | None = None,
    replace_handlers: bool = True,
    dump_last_n: int = 50,
    flush_interval: float = DEFAULT_FLUSH_INTERVAL,
) -> LumberjackHandler:
    """Install lumberjack on the root logger.

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
    global _session
    if _session is not None:
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
    except BaseException:
        if owns_store:
            resolved_store.close()
        raise

    # Everything that can fail is done; the root logger is only touched once
    # the install is guaranteed to complete, so there is no half-swapped state
    # to unwind here.
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
    teardown.install(session)

    for existing in session.prev_handlers:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    _session = session

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
    global _session
    session = _session
    if session is None:
        return
    if session.pump is not None:
        session.pump.stop()
    flush()
    # A live display owns a timer thread and the terminal; leaving it running
    # past shutdown() would leak both.
    session.renderer.close()
    teardown.uninstall()
    root = logging.getLogger()
    root.removeHandler(session.handler)
    for h in session.prev_handlers:
        root.addHandler(h)
    root.setLevel(session.prev_level)
    if session.owns_store:
        session.store.close()
    _session = None


def flush() -> None:
    """Drain the handler's buffer into the store on demand."""
    session = _session
    if session is None:
        return
    rows = session.handler.drain()
    if rows:
        session.store.append(rows)


def is_initialized() -> bool:
    """True between a successful `init()` and the matching `shutdown()`."""
    return _session is not None


def current_handler() -> LumberjackHandler | None:
    """The installed handler, or None if `init()` hasn't run."""
    return _session.handler if _session is not None else None


def current_store() -> RecordStore | None:
    """The store records are being written to, or None if `init()` hasn't run.

    The supported way to query captured records: `current_store().recent()`.
    """
    return _session.store if _session is not None else None


def current_renderer() -> Renderer | None:
    """The renderer chosen for the detected output mode, or None."""
    return _session.renderer if _session is not None else None


def current_output_mode() -> OutputMode | None:
    """The output mode `init()` resolved to, after override/env/TTY detection."""
    return _session.output_mode if _session is not None else None
