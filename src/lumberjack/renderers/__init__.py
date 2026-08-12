"""Rendering interface and factory.

Only `renderers/rich_renderer.py` may import `rich`, and only inside a
try/except guard — this is what makes the bare (no-`rich`) install degrade
to the plain renderer instead of failing at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TextIO, runtime_checkable

from lumberjack.detect import OutputMode

if TYPE_CHECKING:
    from lumberjack.schema import LogRecordRow
    from lumberjack.store import RecordStore


@runtime_checkable
class Renderer(Protocol):
    #: True when every record reaching `render()` is written out verbatim.
    #: Teardown's diagnostic dump exists to recover records a *lossy* live
    #: display swallowed; replaying them for a write-through renderer would
    #: just print the whole session twice. Renderers that redraw in place
    #: (progress bars) must declare False.
    write_through: bool

    def render(self, row: LogRecordRow) -> None: ...
    def close(self) -> None: ...


def rich_available() -> bool:
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


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
    on the write-through plain renderer.
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
