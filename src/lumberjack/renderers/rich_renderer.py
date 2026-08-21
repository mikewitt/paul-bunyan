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
from lumberjack.renderers.plan import (
    Frame,
    FrameCounts,
    HeartbeatLine,
    RowKey,
    plan_frame,
)
from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    DEFAULT_REFRESH_INTERVAL,
    HEARTBEAT_FRAMES_ASCII,
    PASSTHROUGH_LEVEL,
    BarState,
    LoopRow,
    LoopRowModel,
    SessionHeartbeat,
    TaskProgressModel,
    ascii_fallback,
    heartbeat_frames,
)
from lumberjack.renderers.rich_compat import (
    _COLLAPSED_BAR,
    _COLLAPSED_BAR_ASCII,
    _SUBROW,
    _plan_fields,
    _relayout,
    _RowBarColumn,
    _RowElapsedColumn,
    _RowTextColumn,
    _set_total,
)
from lumberjack.schema import LogRecordRow

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import RenderableType

    from lumberjack.renderers.plan import PlanRow
    from lumberjack.store import RecordStore

_LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


#: Separates the facts inside one cell — the heartbeat's count from its rate,
#: and a loop row's cycle position from its iteration count. Chosen once at
#: renderer construction against the console's encoding, exactly as the
#: collapsed-row mark is, because a character lumberjack draws itself must
#: degrade rather than raise on a stream that cannot carry it (issue #70).
_SEPARATOR = "·"
_SEPARATOR_ASCII = "-"


def _format_heartbeat(line: HeartbeatLine) -> Text:
    """The session row: is anything arriving, how fast, and what was it.

    Three facts and no fourth. There is deliberately no elapsed clock and no
    "idle" label — both would keep changing, or keep asserting, while the
    stream said nothing, and the row's whole value is that it goes still when
    the records do.

    Assembly only: every string here was decided by `plan_frame()`, including
    the glyph, which had to be resolved against the console's encoding before
    the frame could be planned.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{line.glyph}  ", style="progress.spinner")
    text.append(line.summary, style="progress.description")
    if line.message:
        text.append(line.message, style="dim")
    return text


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
        clock: Callable[[], float] | None = None,
    ) -> None:
        if Progress is None:
            raise RuntimeError("rich is not installed") from _RICH_IMPORT_ERROR
        # `clock` is threaded to the models rather than held here: nothing in
        # the renderer reads a clock, and a test that needs to cross the idle
        # threshold has to drive the one the models measure against. It exists
        # because the alternative was a test *writing* `renderer._model`, which
        # rebuilt it without the heartbeat and silently lost that row (#71).
        extra = {} if clock is None else {"clock": clock}
        self._model = LoopRowModel(
            store,
            min_repeats=min_repeats,
            **extra,
            # The heartbeat rides on this model's poll, but what it echoes is
            # this renderer's business: it must not repeat a line this
            # renderer already printed above the bars in full.
            # No clock here, deliberately: the heartbeat derives its period
            # from record timestamps rather than from wall time, so there is
            # nothing for a test to drive.
            heartbeat=SessionHeartbeat(store, passthrough_level=passthrough_level),
        )
        self._task_model = TaskProgressModel(store)
        self.passthrough_level = passthrough_level
        # None here means "consult the environment", not "no ceiling" — see
        # resolve_max_bars(). The model still tracks every source either way;
        # this only bounds what gets drawn.
        self._max_bars = resolve_max_bars(max_bars)
        self._suppressed_bars = 0
        #: What the last `refresh()` decided. None before the first one, which
        #: is the state `close()` draws its final frame from when nothing was
        #: ever logged.
        self._frame: Frame | None = None
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
            # The label is the only cell carrying arbitrary user text, so it
            # is the only one given a width-relative cap — see
            # `LABEL_WIDTH_SHARE`. A lambda rather than the width itself
            # because the column is read once per frame, which is what makes
            # the cap follow a resized terminal for free.
            _RowTextColumn(width_of=lambda: self._console.width),
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
        # Both keyed on `PlanRow.key`, a `(RowKind, …)` pair. The kind is what
        # separates a loop row from the position row beneath it: a position
        # row has no identity of its own and borrows the loop's `SourceKey`,
        # which the model guarantees never migrates.
        self._tasks: dict[RowKey, TaskID] = {}
        self._task_bars: dict[RowKey, TaskID] = {}
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
        """Re-read the store, plan the frame, map it onto rich. Timer-driven.

        Two steps that used to be one. `plan_frame()` decides every row, its
        label, its numbers and its state; `_sync()` below only registers and
        updates rich `Task`s from that answer. Nothing between the two makes a
        display decision, which is what lets `tests/test_display_parity.py`
        assert that rich holds exactly what the frame planned — and what stops
        the test recorder from being a second implementation that agrees with
        itself while disagreeing with the screen.
        """
        if self._closed:
            return
        frame = plan_frame(
            rows=self._model.poll(),
            tasks=self._task_model.poll(),
            heartbeat=self._model.heartbeat,
            frames=self._frames,
            separator=self._separator,
            max_bars=self._max_bars,
        )
        self._frame = frame
        self._suppressed_bars = frame.counts.suppressed_loops
        for row in frame.tasks:
            self._sync(self._task_progress, self._task_bars, row)
        drawn = [
            self._sync(self._source_progress, self._tasks, row) for row in frame.rows
        ]
        # Structure, then position. A row is registered wherever the model
        # first reported it, which for a nested loop is always before its
        # parent exists — an inner line logs N times per outer iteration, so
        # it qualifies first every time. Re-laying-out here is what stops that
        # row being indented under whichever unrelated loop happened to
        # precede it. The frame's order is a function of structure alone, so
        # this is a no-op on every poll where nothing structural moved.
        _relayout(self._source_progress, drawn)
        # `update()` and not `refresh()`: the frame is drawn once, below.
        self._live.update(self._compose())
        self._live.refresh()

    @property
    def frame(self) -> Frame | None:
        """What the last `refresh()` decided to draw, or None before the first.

        The decisions, rather than the pixels. Reading it is how a test asks
        "what was this told to draw" without rendering and parsing text back —
        which cannot distinguish a pulse from a full bar, since rich clamps a
        stale `completed > total` and the two render identically.
        """
        return self._frame

    def counts(self) -> FrameCounts:
        """How many bars there are, counted off **rich's own tasks**.

        Deliberately not `self._frame.counts`, and the difference is the whole
        point: computed from what rich actually holds, this answers "is the
        screen what the plan said" rather than "does the plan agree with
        itself". `tests/test_display_parity.py` asserts the two are equal, and
        that assertion is worth nothing the moment this becomes a delegation.

        Do not "simplify" this into `return self._frame.counts`.
        """
        frame = self._frame
        source_tasks = self._source_progress.tasks
        subrows = sum(1 for task in source_tasks if task.fields.get(_SUBROW))
        return FrameCounts(
            sources=frame.counts.sources if frame is not None else 0,
            loops=frame.counts.loops if frame is not None else 0,
            drawn_loops=len(source_tasks) - subrows,
            suppressed_loops=self._suppressed_bars,
            positions=subrows,
            tasks=len(self._task_progress.tasks),
            heartbeat=1 if self._model.heartbeat.events else 0,
        )

    def _sync(
        self,
        progress: Progress,
        registry: dict[RowKey, TaskID],
        row: PlanRow,
    ) -> TaskID:
        """Register or update one rich `Task` from one planned row.

        The only place a `Task` is created, so the key freezing that
        `plan_frame()` does is what keeps an elapsed clock alive: a key that
        moved would be a new `Task`, and a new `Task` silently restarts the
        clock of a loop that has been running for ten minutes.
        """
        task_id = registry.get(row.key)
        if task_id is None:
            # Splatted, not passed as `fields=`: `add_task` collects `**fields`
            # itself, so a literal `fields=` kwarg stores one entry named
            # "fields" — the trap `_plan_fields` documents — and
            # `task.fields["count"]` reads nothing until an `update()` happens
            # to set it.
            task_id = progress.add_task(row.label, total=row.total, **_plan_fields(row))
            registry[row.key] = task_id
        # Via `_set_total` rather than `update(total=...)`, which cannot
        # withdraw a total. Both directions matter: a promoted bar that
        # overruns has to go back to pulsing, a collapsed row that resumes has
        # to shed the total that filling it in implied, and a task that
        # overshoots does the same.
        _set_total(progress, task_id, row.total)
        progress.update(
            task_id,
            description=row.label,
            completed=row.completed,
            **_plan_fields(row),
        )
        if row.done:
            # Freezes the elapsed column. rich only latches it when
            # `completed >= total`, which an under-delivering task never
            # reaches.
            progress.stop_task(task_id)
        return task_id

    def _compose(self) -> Group:
        """The frame: heartbeat, then named bars, then inferred ones.

        The heartbeat appears only once something has arrived — `plan_frame()`
        decides that, and hands back None when it has not. A row reading
        "0 events" is true and worth nothing. A session whose only records are
        task events therefore shows no heartbeat either, which is right: those
        have exact bars of their own.
        """
        rows: list[RenderableType] = []
        if self._frame is not None and self._frame.heartbeat is not None:
            rows.append(_format_heartbeat(self._frame.heartbeat))
        rows += [self._task_progress, self._source_progress]
        return Group(*rows)

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
