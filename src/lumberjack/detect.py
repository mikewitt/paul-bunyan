"""Detect whether output should be interactive (rich) or plain/structured.

Never assume a human is watching: live-redraw output is only appropriate on
a real TTY. Detection can be overridden explicitly (constructor arg) or via
the LUMBERJACK_OUTPUT_MODE environment variable.

The two overrides fail differently on purpose. A bad constructor argument is
a bug in the calling program, so it raises. A bad environment variable is a
typo by whoever launched the process — `RICH`, or a stray trailing space —
and taking the application down over it would be absurd, so names are
normalised and anything still unrecognised warns and falls back to plain.

`resolve_max_bars()` (LUMBERJACK_MAX_BARS) lives here too, rather than in the
renderer that reads it, because it is env-var parsing under the identical
warn-and-degrade policy — a second instance of the same job, not a separate
one.
"""

from __future__ import annotations

import enum
import os
import sys
import warnings
from typing import TextIO

_ENV_VAR = "LUMBERJACK_OUTPUT_MODE"

#: Opt-in ceiling on how many progress-display rows get drawn. Unset means no
#: ceiling.
#:
#: Deliberately an environment variable rather than an `init()` option, and
#: deliberately absent from the README: it is a debug and terminal-compat aid,
#: not something to reach for in production. Capping was always the wrong
#: answer to a high row count, because a high row count was a *symptom* — the
#: display had inherited its unit from the grouping key and was drawing one row
#: per call site. `renderers.progress.loops.LoopRowModel` treats that instead,
#: by merging sibling call sites into the loop they narrate; what remains is
#: allocated by relevance rather than truncated. This stays for the terminal
#: that cannot cope regardless.
MAX_BARS_ENV_VAR = "LUMBERJACK_MAX_BARS"


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


def resolve_max_bars(override: int | None = None) -> int | None:
    """The bar ceiling: `override`, else the environment, else None.

    Lives beside `OutputModeDetector` rather than in the renderer that reads
    it, because it is the same job under a different name: environment-
    variable parsing with warn-and-degrade policy, not a rendering decision.
    Follows the identical split: an out-of-range argument is a caller's bug
    and raises, while a bad environment variable is a typo by whoever
    launched the process, so it warns and carries on uncapped.
    """
    if override is not None:
        if override <= 0:
            raise ValueError(f"max_bars must be positive, got {override!r}")
        return override

    raw = os.environ.get(MAX_BARS_ENV_VAR)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0  # falls into the warning below
    if value <= 0:
        warnings.warn(
            f"{MAX_BARS_ENV_VAR}={raw!r} is not a positive integer; "
            f"drawing every bar.",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    return value
