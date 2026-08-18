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


def _isoformat(created: float) -> str:
    """`created` as a timezone-aware ISO 8601 string, to milliseconds.

    Aware, not naive, and that is the whole point of this function. This
    renderer is the designated safe choice for files and pipes, so its output
    is the output that gets shipped somewhere else and read later — and a
    bare local timestamp cannot be ordered against one from another machine,
    or against itself across a DST boundary.

    Local time rather than UTC, with the offset attached. `.astimezone()` on
    an aware UTC value converts to the host's zone, so someone tailing a file
    still reads their own clock, and the offset makes it unambiguous anyway.
    Emitting UTC would fix the ambiguity too, but by also changing what a
    human sees — a second change this defect does not call for.

    Building the aware UTC value first, rather than `fromtimestamp(created)`
    and letting `.astimezone()` assume local, matters for exactly one hour a
    year. The two agree everywhere except inside a DST fold, where a naive
    local time is genuinely ambiguous and `.astimezone()` has to guess which
    side of the repeated hour it is on. Going through UTC has no ambiguity to
    resolve. Untested, deliberately: reproducing it needs a fold in the
    *host's* zone, and the seam to inject one would exist only for the test.
    """
    return (
        datetime.datetime.fromtimestamp(created, tz=datetime.UTC)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


class PlainTextRenderer:
    """One line per record, as text or as JSON, written and flushed at once.

    Both non-TTY output modes are this one class — `json_lines` picks the
    encoding and nothing else differs — and teardown borrows it again for the
    exit dump, since replaying a lossy display's tail wants exactly this
    format.

    Write-through is the cost model, and it is the opposite of the live bar's:
    a bar repaints on its own timer, so what it costs the calling thread is
    almost nothing, while the write and the flush here are paid for inside the
    `log.debug()` that reached them. Off a terminal there is no frame to
    collapse a record into — nothing is redrawn in place — so every record has
    to become a line, and batching would delay the line without saving it.
    """

    write_through = True

    def __init__(
        self, *, stream: TextIO | None = None, json_lines: bool = False
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.json_lines = json_lines

    def render(self, row: LogRecordRow) -> None:
        """Write the row out whole — every field in JSON mode, a line in text.

        Nothing is filtered by level and nothing is collapsed, which is what
        `write_through` promises and why teardown's exit dump must not replay
        on top of this. Text mode carries the four fields a person scans and
        drops the rest; a traceback follows on its own lines rather than
        being flattened into the message, and in JSON mode it is `exc_text`
        like any other field.
        """
        if self.json_lines:
            # asdict() deepcopies every field, per record.
            # lumberjack: see issue #18
            payload = dataclasses.asdict(row)
            # `created` stays the raw epoch float, because a machine consumer
            # wants to compare and bucket without parsing anything. The ISO
            # string is added beside it rather than instead of it, so a line
            # is readable by a person and by a log pipeline without either
            # having to convert.
            payload["timestamp"] = _isoformat(row.created)
            line = json.dumps(payload, default=str, separators=(",", ":"))
        else:
            ts = _isoformat(row.created)
            line = f"{ts} {row.level_name:<8} {row.logger_name} - {row.message}"
            if row.exc_text:
                line += f"\n{row.exc_text}"
        self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        """Nothing to release: the stream is borrowed, never opened here.

        `sys.stderr` by default, otherwise whatever the caller passed —
        closing either would take down output this renderer does not own, and
        every record has already been flushed as it arrived.

        No closed flag either, unlike the rich renderers: records can still
        arrive after this (`shutdown()` closes before it removes the handler),
        and writing one more line to a borrowed stream is harmless whenever it
        happens.
        """
