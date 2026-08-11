"""Rendering interface and factory.

Only `renderers/rich_renderer.py` may import `rich`, and only inside a
try/except guard — this is what makes the bare (no-`rich`) install degrade
to the plain renderer instead of failing at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TextIO

from lumberjack.detect import OutputMode

if TYPE_CHECKING:
    from lumberjack.schema import LogRecordRow


class Renderer(Protocol):
    def render(self, row: LogRecordRow) -> None: ...
    def close(self) -> None: ...


def rich_available() -> bool:
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def create_renderer(mode: OutputMode, *, stream: TextIO | None = None) -> Renderer:
    if mode is OutputMode.RICH and rich_available():
        from lumberjack.renderers.rich_renderer import RichTerminalRenderer

        return RichTerminalRenderer(stream=stream)

    from lumberjack.renderers.plain import PlainTextRenderer

    return PlainTextRenderer(stream=stream, json_lines=(mode is OutputMode.JSON))
