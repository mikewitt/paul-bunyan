"""atexit/excepthook plumbing.

A live display must be torn down before Python's excepthook prints a
traceback, and before interpreter shutdown — otherwise ANSI/cursor control
corrupts it. This module owns that ordering, plus a best-effort store flush
and an atexit dump of the last N records.

The dump is a recovery path for *lossy* renderers only: a progress bar
collapses a thousand records into one line, so replaying the tail at exit is
the only way to see them. Write-through renderers declare themselves and are
skipped, since replaying there would print the whole session twice.

The exit order is drain-then-dump: the dump reads `store.recent(n)`, so the
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
    from lumberjack.session import Session

_session: Session | None = None
_prev_excepthook = None


def install(session: Session) -> None:
    """Take ownership of `sys.excepthook` and the atexit hook for `session`.

    Raises RuntimeError if already installed, rather than keeping the first
    session and discarding this one in silence. There is only one excepthook
    and one process exit to own, so a second caller is not asking for a
    no-op — it is asking for something this module cannot give it, and
    returning None either way left it no way to find out.

    `uninstall()` is deliberately not symmetrical: undoing nothing is a
    coherent request, which is the same split `init()` and `shutdown()` make.
    """
    global _session, _prev_excepthook
    if _session is not None:
        raise RuntimeError(
            "lumberjack teardown is already installed; call uninstall() first"
        )
    _prev_excepthook = sys.excepthook
    _session = session
    sys.excepthook = handle_exception
    atexit.register(run)


def uninstall() -> None:
    """Give the excepthook and atexit hook back. A no-op if not installed."""
    global _session, _prev_excepthook
    if _session is None:
        return
    sys.excepthook = _prev_excepthook or sys.__excepthook__
    atexit.unregister(run)
    _session = None
    _prev_excepthook = None


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
    if _session is None:
        return
    try:
        _session.renderer.close()
    except Exception:
        pass


def _report_dropped() -> None:
    """Say so if the write buffer overflowed: the store is missing records.

    Unconditional, unlike the dump below — this is about what never reached
    the store, so it is just as true for a write-through renderer.
    """
    if _session is None:
        return
    try:
        dropped = _session.handler.dropped
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
    if _session is None or _session.dump_last_n <= 0:
        return
    # Unknown renderers are assumed lossy: replaying is noisy, but silently
    # dropping the only copy of a record is worse.
    if getattr(_session.renderer, "write_through", False):
        return
    try:
        rows = _session.store.recent(n=_session.dump_last_n)
        dumper = PlainTextRenderer(stream=sys.stderr)
        for row in rows:
            dumper.render(row)
    except Exception:
        pass


def _flush_buffer() -> None:
    if _session is None:
        return
    try:
        rows = _session.handler.drain()
        if rows:
            _session.store.append(rows)
    except Exception:
        pass
