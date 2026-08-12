"""Rich-backed terminal renderers.

This is the only module in the package allowed to import `rich`, and it
does so guarded by try/except so importing `lumberjack.renderers` (and thus
`lumberjack` itself) never requires `rich` to be installed.

Two renderers live here:

* `RichTerminalRenderer` — one styled line per record, still write-through.
* `RichProgressRenderer` — the Phase 1 live bar: repeating source locations
  become bars that advance instead of a thousand scrolling lines.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, TextIO

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        Progress,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.text import Text

    _RICH_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only without rich installed
    Console = None  # type: ignore[assignment,misc]
    Progress = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH_IMPORT_ERROR = exc

from lumberjack.pump import FlushPump
from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    DEFAULT_REFRESH_INTERVAL,
    BarState,
    RepeatingSourceModel,
)
from lumberjack.schema import LogRecordRow, SourceKey

if TYPE_CHECKING:
    from lumberjack.store import RecordStore

_LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


def _format_record(row: LogRecordRow) -> Text:
    style = _LEVEL_STYLES.get(row.level_name, "")
    text = Text()
    text.append(f"{row.level_name:<8} ", style=style)
    text.append(f"{row.logger_name} - {row.message}")
    return text


class RichTerminalRenderer:
    # One line per record — no in-place redraw, so nothing is lost and
    # teardown must not replay. The live bar below is the lossy one.
    write_through = True

    def __init__(self, *, stream: TextIO | None = None) -> None:
        if Console is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        self._console = Console(file=stream if stream is not None else sys.stderr)
        self._closed = False

    def render(self, row: LogRecordRow) -> None:
        if self._closed:
            return
        self._console.print(_format_record(row))
        if row.exc_text:
            self._console.print(row.exc_text, style="red")

    def close(self) -> None:
        self._closed = True


class RichProgressRenderer:
    """Live progress bars, one per repeating source location.

    The Phase 1 crude proof of the premise: a `logger.info(...)` inside a loop
    stops scrolling and becomes a bar that advances. Counts come from the
    store (see `RepeatingSourceModel`), never from this renderer's own
    callback — which is what lets a bar and a plain log file describe the same
    run without divergent logic.

    Lossy by construction: routine records are collapsed into a count rather
    than printed, so `write_through` is False and teardown replays the tail of
    the store at exit. Records at `passthrough_level` and above still print
    above the bars, because a swallowed ERROR is never the right trade.
    """

    write_through = False

    def __init__(
        self,
        store: RecordStore,
        *,
        stream: TextIO | None = None,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        window_seconds: float | None = None,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        passthrough_level: int = logging.WARNING,
    ) -> None:
        if Progress is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        self._model = RepeatingSourceModel(
            store, min_repeats=min_repeats, window_seconds=window_seconds
        )
        self.passthrough_level = passthrough_level
        self._progress = Progress(
            # markup=False: the label is a file path, and a stray "[" in one
            # must not be parsed as a rich tag.
            TextColumn(
                "{task.description}", style="progress.description", markup=False
            ),
            BarColumn(),
            TextColumn("{task.completed} records"),
            TimeElapsedColumn(),
            console=Console(file=stream if stream is not None else sys.stderr),
            # Redraws come from our own timer below, so rich never spins up a
            # second refresh thread racing it.
            auto_refresh=False,
        )
        self._tasks: dict[SourceKey, TaskID] = {}
        self._closed = False
        self._progress.start()
        # A timer, not a record counter: log volume must not drive redraws.
        # interval 0 means "no timer" — callers drive `refresh()` themselves.
        self._pump = (
            FlushPump(
                interval=refresh_interval,
                flush=self.refresh,
                name="lumberjack-progress",
            )
            if refresh_interval > 0
            else None
        )
        if self._pump is not None:
            self._pump.start()

    def render(self, row: LogRecordRow) -> None:
        """Per-record hook. Deliberately does *not* feed the bars.

        The bars are fed by the store on a timer; all this decides is whether
        a record is important enough to also print above them. Everything else
        is collapsed — still in the store, and still replayed by teardown's
        exit dump.
        """
        if self._closed or row.level_no < self.passthrough_level:
            return
        self._progress.console.print(_format_record(row))
        if row.exc_text:
            self._progress.console.print(row.exc_text, style="red")

    def refresh(self) -> None:
        """Re-read the store and redraw. Timer-driven, never per record."""
        if self._closed:
            return
        for bar in self._model.poll():
            task_id = self._tasks.get(bar.source)
            if task_id is None:
                # total=None → an indeterminate bar: this proof knows how many
                # records have arrived, never how many are still coming.
                task_id = self._progress.add_task(bar.label, total=None)
                self._tasks[bar.source] = task_id
            self._progress.update(task_id, completed=bar.count)
        self._progress.refresh()

    def bars(self) -> list[BarState]:
        """What the display is currently advertising, as of the last refresh."""
        return self._model.bars()

    def close(self) -> None:
        """Stop the timer and tear the live display down. Idempotent.

        Ordering matters: teardown calls this before Python's excepthook
        prints, so the timer must be stopped and the cursor restored before a
        traceback reaches the terminal — otherwise a redraw lands on top of it.
        """
        if self._closed:
            return
        # Stop the timer first so nothing redraws behind this, then draw one
        # last frame: the counts a run finished on are the interesting ones.
        if self._pump is not None:
            self._pump.stop()
        try:
            self.refresh()
        except Exception:
            # A store closed ahead of us must not cost the user their terminal
            # (or mangle a traceback) — stopping the display matters more.
            pass
        self._closed = True
        self._progress.stop()
