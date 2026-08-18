"""Rendering interface and factory.

Only `renderers/rich_renderer.py` may import `rich`, and only inside a
try/except guard — this is what makes the bare (no-`rich`) install degrade
to the plain renderer instead of failing at import time.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Protocol, TextIO, runtime_checkable

from lumberjack.detect import OutputMode

if TYPE_CHECKING:
    from lumberjack.schema import LogRecordRow
    from lumberjack.store import RecordStore


@runtime_checkable
class Renderer(Protocol):
    """What `init()` requires of an output layer: two methods and a flag.

    Conformance is structural — nothing in the package inherits from this,
    so an implementation's members cannot be marked `@override`; the
    `runtime_checkable` decorator is what lets a test ask `isinstance()`
    anyway. Teardown is looser still and reads `write_through` by `getattr`
    with a default of False, so an object missing it is treated as lossy.
    """

    #: True when every record reaching `render()` is written out verbatim.
    #: Teardown's exit dump recovers records a *lossy* display swallowed;
    #: replaying them for a write-through renderer would print the session
    #: twice. Renderers that redraw in place must declare False.
    write_through: bool

    def render(self, row: LogRecordRow) -> None:
        """Called once per captured record, from inside the `logging` call.

        Synchronous and on whichever thread logged, so a write-through
        implementation spends the caller's time and a redrawing one must do
        almost nothing here and repaint on its own timer instead. Raising is
        survivable but costly: the handler catches it and routes to stdlib's
        `handleError()`, and the record — already buffered — reaches the
        store regardless, so only the live view is lost.
        """

    def close(self) -> None:
        """Stop accepting records and bring down anything drawn in place.

        Not a stream release: every implementation borrows its stream from the
        caller, or falls back to `sys.stderr`, and closes neither. What comes
        down is the display — a live one has a timer thread to stop and cursor
        state to give back, and the excepthook calls this with a traceback
        seconds away, so that has to happen before the traceback prints.

        Called more than once, and called while the handler is still
        installed: the `atexit` hook closes again after the excepthook already
        has, and `shutdown()` closes before it removes the handler, so a
        thread logging in that window still reaches `render()`. So a second
        close must be a no-op, and a record arriving after one must not raise
        — which is what the rich renderers' `_closed` flags are for.
        """


def rich_available() -> bool:
    """Whether `rich` is on the import path, answered without importing it.

    `find_spec` reports what the import system would find and does not execute
    the module. Both callers import `rich` immediately afterwards when the
    answer is True — `OutputModeDetector` resolving a TTY to RICH, and
    `create_renderer()` importing `rich_renderer` — so it is the False answer
    this exists to produce, in the one place the bare install's fallback to
    the plain renderer is decided.

    A spec on the path is not a promise the import succeeds. That case is
    `rich_renderer`'s to guard, and a renderer constructed after a failed
    import raises RuntimeError rather than degrading.
    """
    return importlib.util.find_spec("rich") is not None


def create_renderer(
    mode: OutputMode,
    *,
    stream: TextIO | None = None,
    store: RecordStore | None = None,
) -> Renderer:
    """Pick a renderer for the detected output mode.

    Only RICH mode — an interactive TTY with `rich` installed — gets the live
    bar, and only when there's a store to read counts from; a bar redrawing
    itself into a pipe or a log file is noise at best. Everything else stays
    on the write-through plain renderer, in JSON-lines form for JSON mode and
    text otherwise. A RICH mode with no `rich` on the path degrades here
    rather than raising, which is Principle 9.

    In RICH mode `store=None` yields `RichTerminalRenderer` — styled lines, no
    bars — and that branch is vestigial: `init()` always passes a store, so
    nothing but a direct caller reaches it. Whether it earns its place is
    issue #45.
    """
    if mode is OutputMode.RICH and rich_available():
        from lumberjack.renderers.rich_renderer import (
            RichProgressRenderer,
            RichTerminalRenderer,
        )

        if store is not None:
            return RichProgressRenderer(store, stream=stream)
        return RichTerminalRenderer(stream=stream)

    from lumberjack.renderers.plain import PlainTextRenderer

    return PlainTextRenderer(stream=stream, json_lines=(mode is OutputMode.JSON))
