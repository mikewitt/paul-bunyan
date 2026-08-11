"""Detect whether output should be interactive (rich) or plain/structured.

Never assume a human is watching: live-redraw output is only appropriate on
a real TTY. Detection can be overridden explicitly (constructor arg) or via
the LUMBERJACK_OUTPUT_MODE environment variable.
"""

from __future__ import annotations

import enum
import os
import sys
from typing import TextIO

_ENV_VAR = "LUMBERJACK_OUTPUT_MODE"


class OutputMode(enum.Enum):
    RICH = "rich"
    PLAIN = "plain"
    JSON = "json"


class OutputModeDetector:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        override: OutputMode | str | None = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.override = self._coerce(override) if override is not None else None

    @staticmethod
    def _coerce(value: OutputMode | str) -> OutputMode:
        if isinstance(value, OutputMode):
            return value
        return OutputMode(value)

    def detect(self) -> OutputMode:
        if self.override is not None:
            return self.override

        env_value = os.environ.get(_ENV_VAR)
        if env_value:
            return self._coerce(env_value)

        isatty = getattr(self.stream, "isatty", None)
        if callable(isatty) and isatty():
            from lumberjack.renderers import rich_available

            if rich_available():
                return OutputMode.RICH
        return OutputMode.PLAIN
