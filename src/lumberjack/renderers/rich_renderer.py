"""Rich-backed terminal renderers.

This module and its sibling `rich_compat.py` are the only ones in the
package allowed to import `rich`, and both do so guarded by try/except so
importing `lumberjack.renderers` (and thus `lumberjack` itself) never
requires `rich` to be installed. `rich_compat.py` holds only the code that
reaches into rich's private state or subclasses a rich internal to work
around a missing public API — `_set_total`, `_relayout`, and the custom
`ProgressColumn`s a loop row draws with; everything else about the display,
however rich-specific, stays here.

Two renderers live here:

* `RichTerminalRenderer` — one styled line per record, still write-through.
  Only reachable by constructing it directly: `create_renderer()` returns it
  when there is no store to read, and `init()` always has one. It is kept for
  that direct use and for tests.
* `RichProgressRenderer` — the live display, and the centrepiece: named bars
  from the tracking API above inferred ones from repeating source locations,
  in place of a thousand scrolling lines.
"""

from __future__ import annotations

import contextlib
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
    BarColumn = None  # type: ignore[assignment,misc]
    _RICH_IMPORT_ERROR = exc

from lumberjack.detect import resolve_max_bars
from lumberjack.pump import FlushPump
from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    DEFAULT_REFRESH_INTERVAL,
    HEARTBEAT_FRAMES_ASCII,
    PASSTHROUGH_LEVEL,
    BarState,
    CyclePosition,
    HeartbeatState,
    LoopRow,
    LoopRowModel,
    SessionHeartbeat,
    TaskProgressModel,
    ascii_fallback,
    heartbeat_frames,
)
from lumberjack.renderers.rich_compat import (
    _COLLAPSED,
    _COLLAPSED_BAR,
    _COLLAPSED_BAR_ASCII,
    _SUBROW,
    _relayout,
    _row_fields,
    _RowBarColumn,
    _RowElapsedColumn,
    _RowTextColumn,
    _set_total,
)
from lumberjack.schema import LogRecordRow, SourceKey

if TYPE_CHECKING:
    from rich.console import RenderableType

    from lumberjack.store import RecordStore

_LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


def _format_rate(row: LoopRow) -> str:
    """How fast a loop is iterating, or what stopped it.

    Rate rather than period because "12/s" is what a reader wants from a
    loop, and sub-1/s loops are the ones where the period is the readable
    form instead. Blank until two records have been seen: one record
    establishes no interval, and a made-up number is worse than none.

    "idle" rather than "done", because idleness is what was measured — no log
    line announces the end of a loop, so a silence long enough to retire the
    bar is the whole of the evidence.
    """
    if row.idle:
        return "idle"
    if row.rate is None:
        return ""
    if row.rate >= 1:
        return f"{row.rate:,.0f}/s"
    return f"{1 / row.rate:,.1f}s each"


#: Separates the facts inside one cell — the heartbeat's count from its rate,
#: and a loop row's cycle position from its iteration count. Chosen once at
#: renderer construction against the console's encoding, exactly as the
#: collapsed-row mark is, because a character lumberjack draws itself must
#: degrade rather than raise on a stream that cannot carry it (issue #70).
_SEPARATOR = "·"
_SEPARATOR_ASCII = "-"

#: How wide the heartbeat's count-and-rate field is padded to, so the message
#: beside it holds one column instead of shuffling sideways every time the
#: count gains a digit. A floor rather than a ceiling: a session busy enough
#: to outgrow it pushes the message right rather than losing any of it.
_HEARTBEAT_SUMMARY_WIDTH = 26


def _format_heartbeat(state: HeartbeatState, frames: str, separator: str) -> Text:
    """The session row: is anything arriving, how fast, and what was it.

    Three facts and no fourth. There is deliberately no elapsed clock and no
    "idle" label — both would keep changing, or keep asserting, while the
    stream said nothing, and the row's whole value is that it goes still when
    the records do.

    The rate carries one decimal where a source bar's carries none. A loop at
    121/s does not need the tenth, but a session ticking over at 2.4/s does:
    rounding that to "2/s" throws away the difference between a program
    creeping along and one that has nearly stopped.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{state.glyph(frames)}  ", style="progress.spinner")
    plural = "" if state.events == 1 else "s"
    summary = f"{state.events:,} event{plural}"
    if state.rate is not None:
        summary += (
            f" {separator} {state.rate:,.1f}/s"
            if state.rate >= 1
            else f" {separator} {1 / state.rate:,.1f}s each"
        )
    text.append(f"{summary:<{_HEARTBEAT_SUMMARY_WIDTH}}", style="progress.description")
    if state.message:
        text.append(state.message, style="dim")
    return text


def _format_source_detail(row: LoopRow, separator: str) -> str:
    """The count column: iterations always, cycle position when inferred.

    **Iterations, not records.** `siblings` reads 400 and not the 1600 log
    calls behind it — every call site there says `row %d`, so the row is the
    unit of work and how many lines narrate each one is the author's choice.
    Record count is an identity-layer number, and the store and the exit
    summary are where it is asked for.

    Both numbers earn their place. The iteration count is the one thing here
    that is certainly true; the cycle position is the inferred part and is what
    the bar's fill is showing, so a reader can see the guess beside the fact.
    """
    iterations = f"{row.count:,} iterations"
    if not row.is_determinate:
        return iterations
    return f"{row.cycle_current}/{row.total} {separator} {iterations}"


def _format_position_detail(position: CyclePosition) -> str:
    """The count column for a position row: which stage of how many.

    "of" rather than the loop row's slash, because the two numbers mean
    different things and should not look alike. `3/5` on a loop row is an
    inferred cycle position — a guess. `3 of 5` here is the AST's count of the
    call sites in a body and the ordinal of the one that just fired, which is
    read rather than measured.
    """
    return f"{position.current} of {position.total}"


def _format_record(row: LogRecordRow) -> Text:
    style = _LEVEL_STYLES.get(row.level_name, "")
    text = Text()
    text.append(f"{row.level_name:<8} ", style=style)
    text.append(f"{row.logger_name} - {row.message}")
    return text


class RichTerminalRenderer:
    """One styled line per record — rich's colour, without rich's bars.

    A Phase 1 vestige. `create_renderer()` reaches it only where there is no
    store to read counts from, and `init()` always passes one, so nothing in
    the package selects it and direct construction — which is tests and
    nothing else — is the only way in. Whether it earns its place at all is
    issue #45.
    """

    # One line per record — no in-place redraw, so nothing is lost and
    # teardown must not replay. The live bar below is the lossy one.
    write_through = True

    def __init__(self, *, stream: TextIO | None = None) -> None:
        if Console is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        self._console = Console(file=stream if stream is not None else sys.stderr)
        self._closed = False

    def render(self, row: LogRecordRow) -> None:
        """Print the record, with any traceback on its own lines beneath it.

        The level picks a style and decides nothing else: nothing is filtered
        and nothing is collapsed, which is what `write_through` promises and
        why teardown's exit dump must not replay on top of this.
        """
        if self._closed:
            return
        self._console.print(_format_record(row))
        if row.exc_text:
            self._console.print(row.exc_text, style="red")

    def close(self) -> None:
        """Stop accepting records. Idempotent.

        Nothing was drawn in place, so there is no cursor state to restore and
        no frame to bring down ahead of a traceback — the whole of closing is
        that `render()` returns early afterwards. The stream stays open: it is
        the caller's, borrowed rather than owned, as it is for every renderer
        here.
        """
        self._closed = True


class RichProgressRenderer:
    """Live progress bars: named exact ones on top, inferred ones below.

    Two kinds, from two models, in one display:

    * **Named bars** (`TaskProgressModel`) come from `task()` and `track()`.
      Every number was stated outright by the instrumented code, so these are
      determinate whenever a total was given and finish on an `end` row.
    * **Loop rows** (`LoopRowModel`) are the inferred ones: a `logger.debug(...)`
      inside a loop stops scrolling and becomes a bar that advances. One row per
      *loop*, not per call site — four lines narrating one loop body are one
      row counting iterations, not four rows counting records. Nothing declared
      them, so they are pulsing counters until the model works out what encloses
      them, determinate once it has, and collapsed when the loop goes quiet.
    * **Position rows**, under a loop row that ticks too slowly to answer "is
      this still running?". Determinate, ticked by the body's call sites in
      the order the source puts them in, and named for the stage that just
      fired. One extra line for a loop that needed one, and nothing at all for
      the loops that did not — see `position.CyclePositionModel`.

    Above both sits the **session heartbeat**, which is neither: one row
    saying whether anything is arriving at all, at what rate, and what the
    last line said. It is the only element a program whose lines never repeat
    can draw, and it is first in the group because the overflow ellipsis crops
    from the bottom and liveness is the row worth keeping.

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
        passthrough_level: int = PASSTHROUGH_LEVEL,
        max_bars: int | None = None,
    ) -> None:
        if Progress is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        self._model = LoopRowModel(
            store,
            min_repeats=min_repeats,
            # The heartbeat rides on this model's poll, but what it echoes is
            # this renderer's business: it must not repeat a line this
            # renderer already printed above the bars in full.
            heartbeat=SessionHeartbeat(store, passthrough_level=passthrough_level),
        )
        self._task_model = TaskProgressModel(store)
        self.passthrough_level = passthrough_level
        # None here means "consult the environment", not "no ceiling" — see
        # resolve_max_bars(). The model still tracks every source either way;
        # this only bounds what gets drawn.
        self._max_bars = resolve_max_bars(max_bars)
        self._suppressed_bars = 0
        self._console = Console(file=stream if stream is not None else sys.stderr)
        # What this terminal can actually take, decided once and from rich's
        # own answers rather than from the raw stream. A Windows console on
        # cp1252 carries neither braille nor `▪`, and an unencodable write
        # raises `UnicodeEncodeError` from inside `emit()` — Principle 9's
        # degrade-never-error, applied to the terminal instead of to a package.
        #
        # Two signals, because they catch different things. `Console.encoding`
        # already defaults to utf-8 for a stream that will not say, which is
        # the convention to match: rich is what does the writing, so agreeing
        # with it is what keeps our glyphs and its box characters consistent.
        # `legacy_windows` is the second: there rich swaps its *own* bars for
        # ASCII, and a row mixing its `-` with our `▪` would be the worst of
        # both.
        legacy = bool(getattr(self._console, "legacy_windows", False))
        if legacy:
            # Said outright rather than by passing a sentinel encoding: `None`
            # means "the stream did not say", which rich reads as utf-8, and
            # routing "degrade" through the same value made this branch draw
            # braille on the one console that cannot take it.
            self._frames = HEARTBEAT_FRAMES_ASCII
            collapsed_mark = _COLLAPSED_BAR_ASCII
            self._separator = _SEPARATOR_ASCII
        else:
            self._frames = heartbeat_frames(self._console.encoding)
            collapsed_mark = ascii_fallback(
                _COLLAPSED_BAR, _COLLAPSED_BAR_ASCII, self._console.encoding
            )
            self._separator = ascii_fallback(
                _SEPARATOR, _SEPARATOR_ASCII, self._console.encoding
            )
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
        # Its own column types rather than rich's, because every cell of a
        # loop row has to know whether the row has collapsed — see
        # `_RowBarColumn`. They are never-markup for the same reason the task
        # bars' columns are: a label is a message template or a file path.
        self._source_progress = Progress(
            _RowTextColumn(),
            _RowBarColumn(collapsed_mark),
            # `completed` drives the bar's fill, which is the *cycle* position
            # once one is inferred, so the counts a reader wants are a field
            # rather than the bar's own numbers.
            _RowTextColumn("detail"),
            _RowTextColumn("rate", style="progress.remaining"),
            _RowElapsedColumn(),
        )
        # The two `Progress` objects are the only *durable* members of the
        # group; `_compose()` rebuilds it on every refresh to put the current
        # heartbeat above them, because that row is a `Text` rather than
        # something rich can re-render from itself. Order there is heartbeat,
        # exact bars, inferred bars: the ellipsis crops from the bottom, so a
        # row nearer the top is the one guaranteed a slot.
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
        # Keyed on the *loop row's* key rather than the position row's own
        # anything: a position row has no identity of its own, and the loop
        # key is the one thing the model guarantees never migrates.
        self._position_tasks: dict[SourceKey, TaskID] = {}
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
        rows = self._model.poll()
        if self._max_bars is not None and len(rows) > self._max_bars:
            self._suppressed_bars = len(rows) - self._max_bars
            # Not `Task.visible = False`: rich skips invisible rows cheaply
            # enough, but `add_task()` refreshes the display on every call, so
            # registering the ones we will never draw costs a redraw each.
            rows = rows[: self._max_bars]
        drawn: list[TaskID] = []
        for row in rows:
            drawn += self._draw_loop_row(row)
        # Structure, then position. `_draw_loop_row` registers a row wherever
        # the model first reported it, which for a nested loop is always before
        # its parent exists — an inner line logs N times per outer iteration,
        # so it qualifies first every time. Re-laying-out here is what stops
        # that row being indented under whichever unrelated loop happened to
        # precede it. The model's order is a function of structure alone, so
        # this is a no-op on every poll where nothing structural moved.
        _relayout(self._source_progress, drawn)
        # `update()` and not `refresh()`: the frame is drawn once, below.
        self._live.update(self._compose())
        self._live.refresh()

    def _compose(self) -> Group:
        """The frame: heartbeat, then named bars, then inferred ones.

        The heartbeat appears only once something has arrived. A row reading
        "0 events" is true and worth nothing — it earns its place by carrying
        a number that moves, and before the first record there is none. A
        session whose only records are task events therefore shows no
        heartbeat either, which is right: those have exact bars of their own.
        """
        heartbeat = self._model.heartbeat
        rows: list[RenderableType] = []
        if heartbeat.events:
            rows.append(_format_heartbeat(heartbeat, self._frames, self._separator))
        rows += [self._task_progress, self._source_progress]
        return Group(*rows)

    def _draw_loop_row(self, row: LoopRow) -> list[TaskID]:
        """One inferred loop: pulsing, determinate, or collapsed.

        Three states, and which one applies is entirely the model's call:

        * **Pulsing** (`total=None`) while nothing bounds the loop. That is
          the permanent state of an outermost loop — nothing encloses it, so
          nothing says how long it is — and the starting state of every other
          one until containment analysis has held an answer for two polls.
        * **Determinate** once an enclosing loop gives the cycle a length.
          The fill shows position *within the current cycle*, so a nested bar
          fills, resets and fills again, while the count column keeps the
          cumulative iteration count.
        * **Collapsed**, when the loop has been quiet for long enough to be
          presumed over. The row is filled and marked: idleness is the only
          completion signal this data has, so acting on it is the claim being
          made, and the rate column says "idle" rather than a stale rate so
          the claim is legible rather than implied. The row stays where it is
          — deleting it would empty the final frame.

        The rich `Task` is keyed on `row.key`, which the model freezes at the
        row's first sighting and never migrates. That is load-bearing rather
        than tidy: a key that moved would be a new `Task`, and a new `Task`
        silently restarts the elapsed clock of a loop that has been running for
        ten minutes.

        Returns the tasks it drew, in the order they belong on screen — the
        loop, then the position row where the loop earned one. The caller
        collects those into the re-layout, which is why this reports them
        rather than being asked again afterwards: a position row is not a row
        of the model's and has no place in the containment order, so nothing
        downstream could work out where it goes.
        """
        label = f"{'  ' * row.depth}{row.label}"
        if row.idle:
            total: int | None = row.count
            completed = row.count
        elif row.is_determinate:
            total, completed = row.total, row.cycle_current
        else:
            total, completed = None, row.count
        task_id = self._tasks.get(row.key)
        if task_id is None:
            task_id = self._source_progress.add_task(
                label, total=total, **_row_fields()
            )
            self._tasks[row.key] = task_id
        # Via `_set_total` rather than `update(total=...)`, which cannot
        # withdraw a total. Both directions matter here: a promoted bar that
        # overruns has to go back to pulsing, and a collapsed row that resumes
        # has to shed the total that filling it in implied.
        _set_total(self._source_progress, task_id, total)
        self._source_progress.update(
            task_id,
            description=label,
            completed=completed,
            rate=_format_rate(row),
            detail=_format_source_detail(row, self._separator),
            **{_COLLAPSED: row.idle},
        )
        if row.position is None:
            return [task_id]
        return [task_id, self._draw_position_row(row, row.position)]

    def _draw_position_row(self, row: LoopRow, position: CyclePosition) -> TaskID:
        """The second row: how far through its body this iteration has got.

        Determinate always, and that is the difference between it and every
        other inferred row here. The loop row pulses because nothing bounds a
        loop; this one is bounded by construction — the body has as many call
        sites as the source says it has, and the ordinal of the one that just
        fired is read from the same place. There is no ratio, no confirmation
        count and nothing to withdraw, so it starts full-width and stays that
        way.

        It exists only because the row above it is too slow to answer "is this
        still running?" — the model decides that, once, and never unmakes it
        (see `position.CyclePositionModel`). So this never has to remove a
        task, which is just as well: removing one would take its slot with it.

        Indented one level past its loop, and collapsing with it. A finished
        loop's last stage is a fact worth keeping on screen — it says where the
        work stopped — but it should stop shouting, exactly as the bar above
        it does.
        """
        # Padded to the widest stage this body can ever show, so the column
        # holds still: a grid column is as wide as its widest cell, and a label
        # that changes length five times an iteration would slide every bar in
        # the display sideways with it.
        label = f"{'  ' * (row.depth + 1)}{position.label:<{position.width}}"
        task_id = self._position_tasks.get(row.key)
        if task_id is None:
            task_id = self._source_progress.add_task(
                label, total=position.total, **_row_fields(subrow=True)
            )
            self._position_tasks[row.key] = task_id
        self._source_progress.update(
            task_id,
            description=label,
            completed=position.current,
            detail=_format_position_detail(position),
            **{_COLLAPSED: row.idle, _SUBROW: True},
        )
        return task_id

    def _refresh_task_bars(self) -> None:
        """Draw what `task()` and `track()` reported. No inference.

        `total=None` gives rich an indeterminate, pulsing bar, which is the
        honest rendering of a task that never said how much work there was.
        A task that did say gets a real percentage.

        A task that *overshoots* its total goes back to pulsing — the
        withdrawal rule, and `_set_total()` is what makes it reach the screen.
        The count column keeps showing the real numbers, so the overshoot is
        visible rather than merely implied.

        A task that *ends* is filled, whatever it claimed on the way. Its
        `end` row is an exact completion signal — the one kind of bar here
        that has one — so leaving an indeterminate task pulsing after it
        would say "still working" about work that is provably over, and would
        leave the elapsed clock running with it.
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
            completed: int = bar.current
            drawn_total: int | None
            if bar.done and bar.total is None:
                # Nothing was ever claimed, so completion can only be
                # expressed as "all of whatever it did". Floored at 1 because
                # a container task — one that only held subtasks and reported
                # no count of its own — is 0 of 0, which rich renders as a
                # full bar labelled 0% and which reads as a failure.
                drawn_total = max(bar.current, 1)
                completed = drawn_total
            elif bar.total is not None and bar.current > bar.total:
                drawn_total = None
            else:
                # A task that ended *short* of a total it claimed keeps both
                # numbers: stopping at 5/10 is a fact, and filling the bar
                # would overwrite it with a claim of 10.
                drawn_total = bar.total
            rich_id = self._task_bars.get(bar.task_id)
            if rich_id is None:
                # Splatted, not passed as `fields=`: `add_task` collects
                # `**fields` itself, so a literal `fields=` kwarg stores one
                # entry literally named "fields" — the exact trap
                # `_row_fields` documents — and `task.fields["count"]` reads
                # nothing until the `update()` below happens to set it.
                rich_id = self._task_progress.add_task(
                    label, total=drawn_total, count=count
                )
                self._task_bars[bar.task_id] = rich_id
            # See `_set_total`: `update(total=None)` would leave a stale total
            # in place, so every withdrawal has to go through it.
            _set_total(self._task_progress, rich_id, drawn_total)
            self._task_progress.update(
                rich_id,
                description=label,
                completed=completed,
                count=count,
            )
            if bar.done:
                # Freezes the elapsed column. rich only latches it when
                # `completed >= total`, which an under-delivering task never
                # reaches.
                self._task_progress.stop_task(rich_id)

    def bars(self) -> list[BarState]:
        """Every *source location* the model is tracking, drawn or not.

        The identity layer, and deliberately not what is on screen: one entry
        per call site, counting records, which is the answer the store would
        corroborate and the one the exit summary prints. `rows()` is the
        display's answer to the same question.

        Unfiltered by the ceiling for the same reason — the ceiling is a
        property of the display, and a caller asking what is being tracked
        should get the honest answer.
        """
        return self._model.bars()

    def rows(self) -> list[LoopRow]:
        """Every loop row, in display order, drawn or not.

        One row per inferred loop rather than per call site, counting
        iterations rather than records. Unfiltered by the ceiling, as `bars()`
        is.
        """
        return self._model.rows()

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
        # A store closed ahead of us must not cost the user their terminal (or
        # mangle a traceback) — stopping the display matters more.
        with contextlib.suppress(Exception):
            self.refresh()
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
