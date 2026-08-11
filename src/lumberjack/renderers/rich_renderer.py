"""Rich-backed terminal renderer.

This is the only module in the package allowed to import `rich`, and it
does so guarded by try/except so importing `lumberjack.renderers` (and thus
`lumberjack` itself) never requires `rich` to be installed.
"""

from __future__ import annotations

import sys
from typing import TextIO

try:
    from rich.console import Console
    from rich.text import Text

    _RICH_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only without rich installed
    Console = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH_IMPORT_ERROR = exc

from lumberjack.schema import LogRecordRow

_LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


class RichTerminalRenderer:
    def __init__(self, *, stream: TextIO | None = None) -> None:
        if Console is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        self._console = Console(file=stream if stream is not None else sys.stderr)
        self._closed = False

    def render(self, row: LogRecordRow) -> None:
        if self._closed:
            return
        style = _LEVEL_STYLES.get(row.level_name, "")
        text = Text()
        text.append(f"{row.level_name:<8} ", style=style)
        text.append(f"{row.logger_name} - {row.message}")
        self._console.print(text)
        if row.exc_text:
            self._console.print(row.exc_text, style="red")

    def close(self) -> None:
        self._closed = True
