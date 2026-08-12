"""atexit/excepthook plumbing.

A live display must be torn down before Python's excepthook prints a
traceback, and before interpreter shutdown — otherwise ANSI/cursor control
corrupts it. This module owns that ordering, plus a best-effort store flush
and an atexit dump of the last N records.

The dump is a recovery path for *lossy* renderers only: a progress bar
collapses a thousand records into one line, so replaying the tail at exit is
the only way to see them. Write-through renderers declare themselves and are
skipped, since replaying there would print the whole session twice.

The exit order is drain-then-dump: the dump reads `store.tail(n)`, so the
handler's buffer has to reach the store first. Every step is wrapped in
try/except — a bug in lumberjack's own cleanup must never hide the user's
real traceback.
"""

from __future__ import annotations

import atexit
import sys
from types import TracebackType
from typing import TYPE_CHECKING

from lumberjack.renderers.plain import PlainTextRenderer

if TYPE_CHECKING:
    from lumberjack.handler import LumberjackHandler
    from lumberjack.renderers import Renderer
    from lumberjack.store import RecordStore

_installed = False
_prev_excepthook = None
_renderer: Renderer | None = None
_handler: LumberjackHandler | None = None
_store: RecordStore | None = None
_dump_last_n = 50


def install(
    *,
    renderer: Renderer,
    handler: LumberjackHandler,
    store: RecordStore,
    dump_last_n: int = 50,
) -> None:
    global _installed, _prev_excepthook, _renderer, _handler, _store, _dump_last_n
    if _installed:
        return
    _prev_excepthook = sys.excepthook
    _renderer = renderer
    _handler = handler
    _store = store
    _dump_last_n = dump_last_n
    sys.excepthook = handle_exception
    atexit.register(run)
    _installed = True


def uninstall() -> None:
    global _installed, _prev_excepthook, _renderer, _handler, _store
    if not _installed:
        return
    sys.excepthook = _prev_excepthook or sys.__excepthook__
    atexit.unregister(run)
    _installed = False
    _prev_excepthook = None
    _renderer = None
    _handler = None
    _store = None


def is_installed() -> bool:
    """True while this module owns sys.excepthook and the atexit hook."""
    return _installed


def current_renderer() -> Renderer | None:
    """The renderer teardown will close, or None if not installed."""
    return _renderer


def handle_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    tb: TracebackType | None,
) -> None:
    """sys.excepthook replacement: kill the display, then print normally."""
    _stop_live_display()
    hook = _prev_excepthook or sys.__excepthook__
    hook(exc_type, exc_value, tb)


def run() -> None:
    """atexit hook: stop the display, drain to the store, dump diagnostics."""
    _stop_live_display()
    _flush_buffer()
    _report_dropped()
    _dump_diagnostics()


def _stop_live_display() -> None:
    if _renderer is not None:
        try:
            _renderer.close()
        except Exception:
            pass


def _report_dropped() -> None:
    """Say so if the write buffer overflowed: the store is missing records.

    Unconditional, unlike the dump below — this is about what never reached
    the store, so it is just as true for a write-through renderer.
    """
    if _handler is None:
        return
    try:
        dropped = _handler.dropped
        if dropped:
            print(
                f"lumberjack: {dropped} record(s) dropped before reaching the "
                "store — the write buffer overflowed. Raise buffer_size or "
                "lower flush_interval in init().",
                file=sys.stderr,
            )
    except Exception:
        pass


def _dump_diagnostics() -> None:
    # Reads the store, never the handler's buffer — see the module docstring:
    # by exit the pump has usually drained the buffer to nothing.
    if _store is None or _dump_last_n <= 0:
        return
    # Unknown renderers are assumed lossy: replaying is noisy, but silently
    # dropping the only copy of a record is worse.
    if getattr(_renderer, "write_through", False):
        return
    try:
        rows = _store.tail(_dump_last_n)
        dumper = PlainTextRenderer(stream=sys.stderr)
        for row in rows:
            dumper.render(row)
    except Exception:
        pass


def _flush_buffer() -> None:
    if _handler is None or _store is None:
        return
    try:
        rows = _handler.drain()
        if rows:
            _store.append(rows)
    except Exception:
        pass
