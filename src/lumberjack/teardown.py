"""atexit/excepthook plumbing.

A live display must be torn down before Python's excepthook prints a
traceback, and before interpreter shutdown — otherwise ANSI/cursor control
corrupts it. This module owns that ordering, plus a best-effort store flush
and an atexit dump of the last N records.

The dump is a recovery path for *lossy* renderers only: a progress bar
collapses a thousand records into one line, so replaying the tail at exit is
the only way to see them. Write-through renderers declare themselves and are
skipped, since replaying there would print the whole session twice.

The exit order is drain first, then everything that reads the store: both the
dump and a live bar's closing frame come from `_session.store`, so the
handler's buffer has to land there before either runs. Every step is wrapped
in try/except — a bug in lumberjack's own cleanup must never hide the user's
real traceback.
"""

from __future__ import annotations

import atexit
import contextlib
import sys
from types import TracebackType
from typing import TYPE_CHECKING

from lumberjack.detect import MAX_BARS_ENV_VAR
from lumberjack.renderers.plain import PlainTextRenderer

if TYPE_CHECKING:
    from lumberjack.schema import LogRecordRow
    from lumberjack.session import Session

_session: Session | None = None
_prev_excepthook = None
#: Rows the exit flush could not write, reported after the display is down.
_unwritten = 0


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
    global _session, _prev_excepthook, _unwritten
    if _session is None:
        return
    sys.excepthook = _prev_excepthook or sys.__excepthook__
    atexit.unregister(run)
    _session = None
    _prev_excepthook = None
    _unwritten = 0


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
    """atexit hook: drain to the store, stop the display, dump diagnostics.

    Drain first. A live bar draws its closing frame from the store, so
    stopping the display before the buffer reaches it leaves the run's final
    count short — or, with the pump disabled, leaves the store empty and the
    bar never drawn at all.

    Stopping the display first is what the *excepthook* path does, and for
    good reason: there a traceback is seconds away and the cursor has to come
    back before it. That urgency does not apply here. By the time this atexit
    hook runs, an exception has already been through `handle_exception()` and
    the display is already down, so `_stop_live_display()` below is a no-op
    on the path that cared.
    """
    _flush_buffer()
    _stop_live_display()
    _report_dropped()
    _report_unwritten()
    _report_display()
    _report_suppressed_bars()
    _dump_diagnostics()


def _stop_live_display() -> None:
    if _session is None:
        return
    with contextlib.suppress(Exception):
        _session.renderer.close()


def _report_dropped() -> None:
    """Say so if the write buffer overflowed: the store is missing records.

    Unconditional, unlike the dump below — this is about what never reached
    the store, so it is just as true for a write-through renderer.
    """
    if _session is None:
        return
    with contextlib.suppress(Exception):
        dropped = _session.handler.dropped
        if dropped:
            print(
                f"lumberjack: {dropped} record(s) dropped before reaching the "
                "store — the write buffer overflowed. Raise buffer_size or "
                "lower flush_interval in init().",
                file=sys.stderr,
            )


def _report_unwritten() -> None:
    """Say so if the exit flush could not reach the store.

    During the run a failed write is retried — `flush()` puts the batch back
    and the next tick tries again. This is the one flush with no next tick,
    so there is nowhere to put the rows and the only honest thing left is to
    name the number. Silence here would be the same defect issue #77
    reported, moved to the one place it is least recoverable.

    Reported separately from `_report_dropped()` rather than folded into it:
    that number means the buffer overflowed and points at `buffer_size` and
    `flush_interval`, which is not the remedy for a store that is refusing
    writes.
    """
    if not _unwritten:
        return
    with contextlib.suppress(Exception):
        print(
            f"lumberjack: {_unwritten} record(s) never reached the store — "
            "the final write failed. The store was unwritable at exit; the "
            "records are lost.",
            file=sys.stderr,
        )


def _report_display() -> None:
    """Say what the display concluded, for a run nobody was watching.

    A live bar is gone the moment the terminal scrolls, and a run in CI never
    had one — so the last frame's own account of itself is the only durable
    answer to "did lumberjack see my loops". Read by duck-typing, like
    `write_through` below: only the live-bar renderer plans frames, and a
    renderer-specific method does not earn a field on `Session`.

    **Two numbers, because they answer different questions.** `sources` is
    call sites, which is what the store would corroborate; `loops` is what a
    reader saw. They disagree by design — four log lines narrating one loop
    are four sources and one row — and reporting only one of them is how the
    display's unit and its identity unit got conflated in the first place.

    Silent when nothing repeated, which is the common case for a short script
    and not worth a line.
    """
    if _session is None:
        return
    with contextlib.suppress(Exception):
        counts = _session.renderer.counts()  # type: ignore[attr-defined]
        if not counts.loops:
            return
        print(
            f"lumberjack: {counts.loops} loop(s) inferred from "
            f"{counts.sources} repeating log source(s); "
            f"{counts.tasks} named task bar(s).",
            file=sys.stderr,
        )


def _report_suppressed_bars() -> None:
    """Say so if a bar ceiling hid part of the display.

    Read by duck-typing, like `write_through` below: only the live-bar
    renderer has a ceiling, and a renderer-specific counter does not earn a
    field on `Session`.

    The remedy deliberately does not say "raise the ceiling". A bar count that
    hits it means log lines are not collapsing into shared shapes, and the
    ceiling hides that rather than fixing it.
    """
    if _session is None:
        return
    with contextlib.suppress(Exception):
        suppressed = getattr(_session.renderer, "suppressed_bars", 0)
        if suppressed:
            print(
                f"lumberjack: {suppressed} progress bar(s) hidden by "
                f"{MAX_BARS_ENV_VAR} — the ceiling is a debug aid, and a bar "
                "count that reaches it means log lines are not collapsing "
                "into shared shapes, which the ceiling hides rather than "
                "fixes.",
                file=sys.stderr,
            )


def _dump_diagnostics() -> None:
    # Reads the store, never the handler's buffer — see the module docstring:
    # by exit the pump has usually drained the buffer to nothing.
    if _session is None or _session.dump_last_n <= 0:
        return
    # Unknown renderers are assumed lossy: replaying is noisy, but silently
    # dropping the only copy of a record is worse.
    if getattr(_session.renderer, "write_through", False):
        return
    with contextlib.suppress(Exception):
        rows = _session.store.recent(n=_session.dump_last_n)
        dumper = PlainTextRenderer(stream=sys.stderr)
        for row in rows:
            dumper.render(row)


def _flush_buffer() -> None:
    # The last chance the buffered records have. `flush()` puts a failed
    # batch back for the next tick to retry; there is no next tick here, so
    # the count is kept for `_report_unwritten()` to name instead. Losing
    # them silently is what issue #77 reported.
    #
    # Deliberately a duplicate of `lumberjack.flush()`, not a call to it:
    # `flush()` takes `session.registry_lock()` around the same read-then-write
    # so a concurrent `shutdown()` cannot close the store between them, but
    # this module keeps its own `_session` reference on its own lifecycle (see
    # session.py's module docstring) rather than going through that registry —
    # by the time an atexit hook or excepthook runs, `shutdown()` racing it is
    # not the failure mode this guards against. Do not "simplify" this into a
    # call to `flush()`.
    global _unwritten
    if _session is None:
        return
    rows: list[LogRecordRow] = []
    try:
        rows = _session.handler.drain()
        if rows:
            _session.store.append(rows)
    except Exception:  # noqa: BLE001 - cleanup must never hide a real traceback
        _unwritten = len(rows)
