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
from lumberjack.renderers import create_renderer
from lumberjack.store import RecordStore, SQLiteRecordStore

try:
    __version__ = importlib.metadata.version("lumberjack")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["init", "shutdown", "flush", "__version__"]

_installed = False
_handler: LumberjackHandler | None = None
_store: RecordStore | None = None
_prev_handlers: list[logging.Handler] = []


def init(
    *,
    level: int = logging.INFO,
    output_mode: OutputMode | str | None = None,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    store: RecordStore | None = None,
    replace_handlers: bool = True,
    dump_last_n: int = 50,
) -> LumberjackHandler:
    """Install lumberjack on the root logger.

    Replaces the root logger's existing handlers by default; pass
    `replace_handlers=False` to layer alongside them instead. Raises
    RuntimeError if already installed — call `shutdown()` first.
    """
    global _installed, _handler, _store, _prev_handlers
    if _installed:
        raise RuntimeError(
            "lumberjack.init() already called; call lumberjack.shutdown() first"
        )

    resolved_store = store if store is not None else SQLiteRecordStore(":memory:")
    mode = OutputModeDetector(override=output_mode).detect()
    renderer = create_renderer(mode)

    handler = LumberjackHandler(
        buffer_size=buffer_size,
        on_record=renderer.render,
        level=level,
    )

    root = logging.getLogger()
    if replace_handlers:
        _prev_handlers = root.handlers[:]
        for existing in _prev_handlers:
            root.removeHandler(existing)
    else:
        _prev_handlers = []
    root.addHandler(handler)
    root.setLevel(level)

    teardown.install(
        renderer=renderer,
        handler=handler,
        store=resolved_store,
        dump_last_n=dump_last_n,
    )

    _handler = handler
    _store = resolved_store
    _installed = True
    return handler


def shutdown() -> None:
    """Tear down lumberjack: flush the buffer, restore prior handlers."""
    global _installed, _handler, _store, _prev_handlers
    if not _installed:
        return
    flush()
    teardown.uninstall()
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    for h in _prev_handlers:
        root.addHandler(h)
    _prev_handlers = []
    _handler = None
    _store = None
    _installed = False


def flush() -> None:
    """Drain the handler's buffer into the store on demand."""
    if _handler is None or _store is None:
        return
    rows = _handler.drain()
    if rows:
        _store.append(rows)
