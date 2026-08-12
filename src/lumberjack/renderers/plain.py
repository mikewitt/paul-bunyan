"""Write-through plain text / JSON-lines renderer.

The safe default for non-TTY output. Never emits ANSI or cursor-control
codes, and writes+flushes per record rather than batching on a timer.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import sys
from typing import TextIO

from lumberjack.schema import LogRecordRow


class PlainTextRenderer:
    write_through = True

    def __init__(
        self, *, stream: TextIO | None = None, json_lines: bool = False
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.json_lines = json_lines

    def render(self, row: LogRecordRow) -> None:
        if self.json_lines:
            # asdict() deepcopies every field, per record.
            # lumberjack: see issue #18
            line = json.dumps(
                dataclasses.asdict(row), default=str, separators=(",", ":")
            )
        else:
            # Naive local time here, raw epoch float in JSON — two
            # representations from one renderer. lumberjack: see issue #14
            ts = datetime.datetime.fromtimestamp(row.created).isoformat(
                timespec="milliseconds"
            )
            line = f"{ts} {row.level_name:<8} {row.logger_name} - {row.message}"
            if row.exc_text:
                line += f"\n{row.exc_text}"
        self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        pass
