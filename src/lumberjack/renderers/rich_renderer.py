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
    from rich.console import Console, Group
    from rich.live import Live
    from rich.progress import (
        BarColumn,
        Progress,
        TaskID,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.text import Text

    _RICH_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only without rich installed
    Console = None  # type: ignore[assignment,misc]
    Group = None  # type: ignore[assignment,misc]
    Live = None  # type: ignore[assignment,misc]
    Progress = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH_IMPORT_ERROR = exc

from lumberjack.pump import FlushPump
from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    DEFAULT_REFRESH_INTERVAL,
    BarState,
    RepeatingSourceModel,
    TaskProgressModel,
    resolve_max_bars,
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


def _format_rate(bar: BarState) -> str:
    """How fast a source is repeating, or what stopped it.

    Rate rather than period because "12/s" is what a reader wants from a
    loop, and sub-1/s loops are the ones where the period is the readable
    form instead. Blank until two records have been seen: one record
    establishes no interval, and a made-up number is worse than none.

    "idle" rather than "done", because idleness is what was measured — no log
    line announces the end of a loop, so a silence long enough to retire the
    bar is the whole of the evidence.
    """
    if bar.idle:
        return "idle"
    if bar.rate is None:
        return ""
    if bar.rate >= 1:
        return f"{bar.rate:,.0f}/s"
    return f"{1 / bar.rate:,.1f}s each"


def _format_source_detail(bar: BarState) -> str:
    """The count column: cumulative always, cycle position when inferred.

    Both numbers earn their place. The cumulative count is the one thing here
    that is certainly true, and it is what the exit dump will corroborate;
    the cycle position is the inferred part and is what the bar's fill is
    showing, so a reader can see the guess beside the fact.
    """
    records = f"{bar.count:,} records"
    if not bar.is_determinate:
        return records
    return f"{bar.cycle_current}/{bar.total} · {records}"


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
    """Live progress bars: named exact ones on top, inferred ones below.

    Two kinds, from two models, in one display:

    * **Named bars** (`TaskProgressModel`) come from `task()` and `track()`.
      Every number was stated outright by the instrumented code, so these are
      determinate whenever a total was given and finish on an `end` row.
    * **Source bars** (`RepeatingSourceModel`) are the inferred ones: a
      `logger.debug(...)` inside a loop stops scrolling and becomes a bar that
      advances. Nothing declared them, so they are pulsing counters until the
      model works out what encloses them, determinate once it has, and
      retired when the loop goes quiet.

    Both read the store rather than this renderer's own callback, so a bar and
    a plain log file describe the same run without divergent logic.

    **One `Live`, two unstarted `Progress` objects.** `Progress.start()` would
    start a `Live` of its own, and rich permits one per console: the second
    becomes nested, at which point its `refresh()` re-renders the *root's*
    renderable and its own bars never draw. Separate consoles are worse — two
    Lives writing cursor control to one stderr corrupt the frame. So this owns
    the `Live` and renders a `Group`, which is also what lets the exact bars
    sit above the inferred ones: the overflow ellipsis crops from the bottom,
    and instrumented bars must not lose their slots to guessed ones.

    Lossy by construction — routine records are collapsed into a count, so
    `write_through` is False and teardown replays the store's tail at exit.
    Records at `passthrough_level` and above still print above the bars: a
    swallowed ERROR is never the right trade.
    """

    write_through = False

    def __init__(
        self,
        store: RecordStore,
        *,
        stream: TextIO | None = None,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        passthrough_level: int = logging.WARNING,
        max_bars: int | None = None,
    ) -> None:
        if Progress is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        self._model = RepeatingSourceModel(store, min_repeats=min_repeats)
        self._task_model = TaskProgressModel(store)
        self.passthrough_level = passthrough_level
        # None here means "consult the environment", not "no ceiling" — see
        # resolve_max_bars(). The model still tracks every source either way;
        # this only bounds what gets drawn.
        self._max_bars = resolve_max_bars(max_bars)
        self._suppressed_bars = 0
        self._console = Console(file=stream if stream is not None else sys.stderr)
        # markup=False throughout: labels carry file paths and user-supplied
        # task names, and a stray "[" in either must not parse as a rich tag.
        self._task_progress = Progress(
            TextColumn(
                "{task.description}", style="progress.description", markup=False
            ),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.fields[count]}", markup=False),
            TimeElapsedColumn(),
        )
        self._source_progress = Progress(
            TextColumn(
                "{task.description}", style="progress.description", markup=False
            ),
            BarColumn(),
            # `completed` drives the bar's fill, which is the *cycle* position
            # once one is inferred, so the counts a reader wants are a field
            # rather than the bar's own numbers.
            TextColumn("{task.fields[detail]}", markup=False),
            TextColumn("{task.fields[rate]}", style="progress.remaining"),
            TimeElapsedColumn(),
        )
        # Exact bars first: the ellipsis crops from the bottom, so inferred
        # bars are the ones that should lose their slots.
        self._live = Live(
            Group(self._task_progress, self._source_progress),
            console=self._console,
            # Redraws come from our own timer below, so rich never spins up a
            # second refresh thread racing it.
            auto_refresh=False,
            # An over-tall frame shows the first N rows plus an ellipsis with
            # correct cursor arithmetic, rather than scrolling the terminal.
            vertical_overflow="ellipsis",
            # rich redirects **both** streams by default, and redirecting
            # stdout is wrong here. `Live.start()` swaps `sys.stdout` for a
            # proxy bound to this console — which writes to *stderr* — so a
            # program doing `app.py > data.txt` would find its `print()` output
            # on the terminal and its file empty, for as long as a bar was on
            # screen. lumberjack owns stderr and must not touch the channel a
            # program uses for its results.
            redirect_stdout=False,
            # stderr is a different question and the default is right: a raw
            # `sys.stderr.write` lands in the middle of a live frame and
            # corrupts it, while routed through the console it prints cleanly
            # above the bars — the same treatment a WARNING record gets.
            redirect_stderr=True,
        )
        self._tasks: dict[SourceKey, TaskID] = {}
        self._task_bars: dict[int, TaskID] = {}
        self._closed = False
        self._live.start()
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

        The store feeds the bars on a timer; all this decides is whether a
        record also prints above them. Everything else is collapsed — still
        stored, still replayed by teardown's exit dump.
        """
        if self._closed or row.level_no < self.passthrough_level:
            return
        self._console.print(_format_record(row))
        if row.exc_text:
            self._console.print(row.exc_text, style="red")

    @property
    def suppressed_bars(self) -> int:
        """Bars the ceiling kept off screen as of the last refresh.

        Teardown reads this to report at exit; 0 when uncapped.
        """
        return self._suppressed_bars

    def refresh(self) -> None:
        """Re-read the store and redraw. Timer-driven, never per record."""
        if self._closed:
            return
        self._refresh_task_bars()
        bars = self._model.poll()
        if self._max_bars is not None and len(bars) > self._max_bars:
            self._suppressed_bars = len(bars) - self._max_bars
            # Not `Task.visible = False`: rich skips invisible rows cheaply
            # enough, but `add_task()` refreshes the display on every call, so
            # registering the ones we will never draw costs a redraw each.
            bars = bars[: self._max_bars]
        for bar in bars:
            self._draw_source_bar(bar)
        self._live.refresh()

    def _draw_source_bar(self, bar: BarState) -> None:
        """One inferred bar: pulsing, determinate, or retired.

        Three states, and which one applies is entirely the model's call:

        * **Pulsing** (`total=None`) while nothing bounds the loop. That is
          the permanent state of an outermost loop — nothing encloses it, so
          nothing says how long it is — and the starting state of every other
          one until containment analysis has held an answer for two polls.
        * **Determinate** once an enclosing loop gives the cycle a length.
          The fill shows position *within the current cycle*, so a nested bar
          fills, resets and fills again, while the count column keeps the
          cumulative total.
        * **Retired**, when the source has been quiet for long enough that
          the loop is presumed over. The bar is filled to mark it finished:
          idleness is the only completion signal this data has, so acting on
          it is the claim being made, and the rate column says "idle" rather
          than a stale rate so the claim is legible rather than implied.
        """
        label = f"{'  ' * bar.depth}{bar.label}"
        if bar.idle:
            total: int | None = bar.count
            completed = bar.count
        elif bar.is_determinate:
            total, completed = bar.total, bar.cycle_current
        else:
            total, completed = None, bar.count
        task_id = self._tasks.get(bar.source)
        if task_id is None:
            task_id = self._source_progress.add_task(
                label, total=total, fields={"rate": "", "detail": ""}
            )
            self._tasks[bar.source] = task_id
        self._source_progress.update(
            task_id,
            description=label,
            total=total,
            completed=completed,
            rate=_format_rate(bar),
            detail=_format_source_detail(bar),
        )

    def _refresh_task_bars(self) -> None:
        """Draw what `task()` and `track()` reported. No inference.

        `total=None` gives rich an indeterminate, pulsing bar, which is the
        honest rendering of a task that never said how much work there was.
        A task that did say gets a real percentage.

        A task that *overshoots* its total goes back to pulsing. rich clamps
        `completed > total` to a full 100% bar, which reads as "finished"
        while the work is still running — the one thing a bar must not say.
        Pulsing withdraws the claim instead, and the count column keeps
        showing the real numbers so the overshoot is visible rather than
        merely implied.
        """
        for bar in self._task_model.poll():
            label = f"{'  ' * bar.depth}{bar.label}"
            if bar.total is not None:
                count = f"{bar.current}/{bar.total}"
            elif bar.current:
                count = str(bar.current)
            else:
                # A task that never reported progress — usually a container
                # for subtasks. A bare "0" beside a pulsing bar reads as
                # "stuck at zero" rather than "no count was claimed".
                count = ""
            drawn_total = (
                None if bar.total is not None and bar.current > bar.total else bar.total
            )
            rich_id = self._task_bars.get(bar.task_id)
            if rich_id is None:
                rich_id = self._task_progress.add_task(
                    label, total=drawn_total, fields={"count": count}
                )
                self._task_bars[bar.task_id] = rich_id
            self._task_progress.update(
                rich_id,
                description=label,
                total=drawn_total,
                completed=bar.current,
                count=count,
            )

    def bars(self) -> list[BarState]:
        """Every bar the model is tracking, drawn or not.

        Deliberately unfiltered by the ceiling: the ceiling is a property of
        the display, and a caller asking what is being tracked should get the
        honest answer.
        """
        return self._model.bars()

    def close(self) -> None:
        """Stop the timer and tear the live display down. Idempotent.

        Teardown calls this before Python's excepthook prints, so the cursor
        must be restored before a traceback reaches the terminal — otherwise a
        redraw lands on top of it.
        """
        if self._closed:
            return
        # Timer first so nothing redraws behind us, then one last frame: the
        # counts a run finished on are the interesting ones.
        if self._pump is not None:
            self._pump.stop()
        try:
            self.refresh()
        except Exception:
            # A store closed ahead of us must not cost the user their terminal
            # (or mangle a traceback) — stopping the display matters more.
            pass
        self._closed = True
        # Deliberately not fixed here, despite this being the line that would
        # do it: `Live.stop()` sets `vertical_overflow = "visible"` itself,
        # with a comment saying it means to, so re-asserting "ellipsis" is not
        # the fix — the renderable has to be bounded before stopping, and
        # *which* bars survive that bound is the open question in #8. Making
        # it transient would crop by deleting the final counts, which the
        # "drain before closing" decision exists to preserve.
        # lumberjack: see issue #28
        self._live.stop()
