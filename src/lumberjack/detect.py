"""Detect whether output should be interactive (rich) or plain/structured.

Never assume a human is watching: live-redraw output is only appropriate on
a real TTY. Detection can be overridden explicitly (constructor arg) or via
the LUMBERJACK_OUTPUT_MODE environment variable.

The two overrides fail differently on purpose. A bad constructor argument is
a bug in the calling program, so it raises. A bad environment variable is a
typo by whoever launched the process — `RICH`, or a stray trailing space —
and taking the application down over it would be absurd, so names are
normalised and anything still unrecognised warns and falls back to plain.
"""

from __future__ import annotations

import enum
import os
import sys
import warnings
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
        """Resolve a mode name. Raises ValueError if it isn't one."""
        if isinstance(value, OutputMode):
            return value
        return OutputMode(value.strip().lower())

    def detect(self) -> OutputMode:
        if self.override is not None:
            return self.override

        env_value = os.environ.get(_ENV_VAR)
        if env_value:
            try:
                return self._coerce(env_value)
            except ValueError:
                valid = ", ".join(mode.value for mode in OutputMode)
                warnings.warn(
                    f"{_ENV_VAR}={env_value!r} is not a valid output mode "
                    f"({valid}); falling back to plain output.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                return OutputMode.PLAIN

        isatty = getattr(self.stream, "isatty", None)
        if callable(isatty) and isatty():
            from lumberjack.renderers import rich_available

            if rich_available():
                return OutputMode.RICH
        return OutputMode.PLAIN
