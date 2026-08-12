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

# Module-level lifecycle state: unguarded by any lock, and mirrored by
# teardown.py's own copies. lumberjack: see issues #11, #17
_installed = False
_handler: LumberjackHandler | None = None
_store: RecordStore | None = None
_owns_store = False
_renderer: Renderer | None = None
_output_mode: OutputMode | None = None
_pump: FlushPump | None = None
_prev_handlers: list[logging.Handler] = []
_prev_level: int | None = None


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
    global _installed, _handler, _store, _owns_store, _prev_handlers, _prev_level
    global _renderer, _output_mode, _pump
    if _installed:
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

        teardown.install(
            renderer=renderer,
            handler=handler,
            store=resolved_store,
            dump_last_n=dump_last_n,
        )
    except BaseException:
        if owns_store:
            resolved_store.close()
        raise

    # Everything that can fail is done; the root logger is only touched once
    # the install is guaranteed to complete, so there is no half-swapped state
    # to unwind here.
    root = logging.getLogger()
    prev_handlers = root.handlers[:] if replace_handlers else []
    for existing in prev_handlers:
        root.removeHandler(existing)
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(level)

    _owns_store = owns_store
    _prev_handlers = prev_handlers
    _prev_level = prev_level
    _handler = handler
    _store = resolved_store
    _renderer = renderer
    _output_mode = mode
    _installed = True

    if flush_interval > 0:
        _pump = FlushPump(interval=flush_interval, flush=flush)
        _pump.start()

    return handler


def shutdown() -> None:
    """Tear down lumberjack: stop the pump, flush, restore the root logger.

    Restores both halves of what `init()` took over — the handler list *and*
    the level — so a library that only wanted lumberjack for part of a run
    does not silently leave the root logger more verbose than it found it.

    Closes the store only if lumberjack created it — a store the caller passed
    to `init()` stays open so it can still be queried afterward.
    """
    global _installed, _handler, _store, _owns_store, _prev_handlers, _prev_level
    global _renderer, _output_mode, _pump
    if not _installed:
        return
    if _pump is not None:
        _pump.stop()
        _pump = None
    flush()
    if _renderer is not None:
        # A live display owns a timer thread and the terminal; leaving it
        # running past shutdown() would leak both.
        _renderer.close()
    teardown.uninstall()
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    for h in _prev_handlers:
        root.addHandler(h)
    if _prev_level is not None:
        root.setLevel(_prev_level)
    if _owns_store and _store is not None:
        _store.close()
    _prev_handlers = []
    _prev_level = None
    _handler = None
    _store = None
    _owns_store = False
    _renderer = None
    _output_mode = None
    _installed = False


def flush() -> None:
    """Drain the handler's buffer into the store on demand."""
    if _handler is None or _store is None:
        return
    rows = _handler.drain()
    if rows:
        _store.append(rows)


def is_initialized() -> bool:
    """True between a successful `init()` and the matching `shutdown()`."""
    return _installed


def current_handler() -> LumberjackHandler | None:
    """The installed handler, or None if `init()` hasn't run."""
    return _handler


def current_store() -> RecordStore | None:
    """The store records are being written to, or None if `init()` hasn't run.

    The supported way to query captured records: `current_store().recent()`.
    """
    return _store


def current_renderer() -> Renderer | None:
    """The renderer chosen for the detected output mode, or None."""
    return _renderer


def current_output_mode() -> OutputMode | None:
    """The output mode `init()` resolved to, after override/env/TTY detection."""
    return _output_mode
