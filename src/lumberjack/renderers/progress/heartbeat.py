"""The session row: whether anything is arriving at all, and what it last said.

Neither a bar nor an inference. It is the one element a program whose log
lines never repeat can still draw, and it rides on the source model's poll
rather than fetching a delta of its own.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from lumberjack.renderers.progress.smoothing import (
    _smoothed,
    advance_watermark,
    fold_interval,
)

if TYPE_CHECKING:
    from lumberjack.store import RecordStore, SourceDelta

#: How far back the heartbeat looks for a line it is willing to echo.
#:
#: More than one because the newest record is often not one to echo: a poll
#: ending on a WARNING or a task event would otherwise fall back on whatever
#: was said before it, which is stale mid-run and nothing at all if that poll
#: is the first. Only a few, because each row is materialized as a dataclass
#: to read one string off it. Measured against a 1M-row `:memory:` store, min
#: of 300: `recent(n=1)` 16µs, `n=4` 51µs, `n=8` 97µs — about 12µs a row —
#: against ~1ms for the delta query beside it. Four keeps this at a twentieth
#: of a redraw's store cost.
MESSAGE_LOOKBACK = 4

#: The heartbeat's frames, advanced one per poll that brought records.
#:
#: Braille dots, the same shape as rich's default spinner and deliberately
#: *not* rich's `Spinner`, which renders from `time.monotonic()` and produces
#: four distinct frames from a stream that carried nothing. Turning on the
#: clock claims liveness nobody observed, which is the one thing this row
#: exists not to do — so the frame is an index into this string and the index
#: only moves when a record does.
HEARTBEAT_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: What to spin with when the output encoding cannot carry braille. A Windows
#: console on `cp1252` encodes neither these dots nor `━` nor `▪`, and a write
#: it cannot encode raises `UnicodeEncodeError` rather than degrading — which
#: would take down the `logger.debug()` that reached it.
#:
#: rich substitutes its *own* box and bar characters when it detects a limited
#: encoding, and cannot know to do the same for a string lumberjack authored,
#: so anything drawn from this module owes its own fallback.
HEARTBEAT_FRAMES_ASCII = "|/-\\"

#: The level at and above which a record prints above the bars in full,
#: rather than being collapsed into the heartbeat's count and last-message
#: fields. `SessionHeartbeat`'s constructor default and
#: `RichProgressRenderer`'s are the same choice stated once: the renderer
#: always threads its own value down to the heartbeat it builds, so this
#: constant's only live use is `SessionHeartbeat(store)` built directly, with
#: no renderer supplying one.
PASSTHROUGH_LEVEL = logging.WARNING


def ascii_fallback(glyphs: str, ascii_glyphs: str, encoding: str | None) -> str:
    """`glyphs` if `encoding` can carry them, `ascii_glyphs` if it cannot.

    Principle 9's rule — degrade, never error — applied to the terminal rather
    than to a package: an unencodable glyph is exactly as fatal as a missing
    dependency, and just as unnecessary. Shared by every character lumberjack
    draws itself, per the module docstring above — `heartbeat_frames()` below
    is one caller, `RichProgressRenderer`'s collapsed-row mark is the other.

    Two signals, handled in the order rich itself would encounter them.
    `encoding is None` is rich's own convention for a stream that did not say,
    which it reads as utf-8 — agreeing with it is what keeps our glyphs and
    its box characters consistent. An empty string is a stream that said
    "nothing" rather than one that said nothing, so it is taken at its word
    and gets the fallback. Anything else is tried for real: `HEARTBEAT_FRAMES`
    and `▪` both fail to encode under `cp1252`, a Windows console's default,
    and an unencodable write raises rather than degrading.
    """
    if encoding is None:
        return glyphs
    if not encoding:
        return ascii_glyphs
    try:
        glyphs.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ascii_glyphs
    return glyphs


def heartbeat_frames(encoding: str | None) -> str:
    """The frame set `encoding` can actually carry. See `ascii_fallback`."""
    return ascii_fallback(HEARTBEAT_FRAMES, HEARTBEAT_FRAMES_ASCII, encoding)


@dataclasses.dataclass(frozen=True, slots=True)
class HeartbeatState:
    """Whether this session is seeing anything at all, and what it last saw.

    The element for a program whose log lines do not repeat. `oneshot` — six
    startup lines, one each — draws no bar, because a source needs repetition
    to have a period, and an empty display is indistinguishable from a hung
    one. That is the first question the display exists to answer, so it gets
    the row that is always available: a count, an arrival rate, and the last
    line, none of which need a line to recur.

    Everything here is a statement about records that arrived. `beat` is the
    load-bearing one: it advances on a poll that brought records and on no
    other, so a silence freezes the glyph rather than animating through it.
    """

    #: Records folded in since the session started. Cumulative and monotonic,
    #: for the same reason a bar's count is: `evict()` trimming the store must
    #: not make the session look less busy than it was.
    events: int = 0
    #: How many polls have brought records. Its one use is as the frame index,
    #: which is why nothing else may move it.
    beat: int = 0
    #: Seconds between arrivals across all sources, smoothed. None until two
    #: records have been seen — one record establishes no interval.
    period: float | None = None
    #: The newest *collapsed* record's message, first line only. See
    #: `SessionHeartbeat._newest_message` for why a record loud enough to
    #: print above the bars is not repeated here.
    message: str | None = None

    @property
    def rate(self) -> float | None:
        """Records per second, or None while the period is not a usable number.

        "Usable" is doing real work here, and the guard is on the *result*
        rather than only on the period. `period <= 0` alone lets three
        unusable values through, all of which reach a display column:

        | `period` | `rate` was | what it did |
        |---|---|---|
        | `inf` | `0.0` | `1 / rate` raised `ZeroDivisionError` |
        | `nan` | `nan` | rendered the string `"nans each"` |
        | `1e-320` | `inf` | overflowed the other way |

        The first is the one that matters, because `FlushPump` wraps the
        redraw in `contextlib.suppress(Exception)` — so a raise inside a
        frame freezes the live display silently and forever rather than
        reporting anything. The guard rejects all three, `nan` included,
        since every comparison against `nan` is False.

        **It does not bound magnitude, and must not be read as doing so.** A
        `period` of 1e308 gives `rate=1e-308` — positive, finite, and past
        every check here. That is a real number honestly reported; what it
        used to do was render as 419 characters and drag the display
        sideways, which is a *width* problem and belongs to the formatter.
        `plan.MAX_SECONDS_WIDTH` is where it is solved.

        None is the honest answer for all of them: a period nothing can be
        computed from is a period that has not been measured, which is what
        None already means here. Returning it keeps this property's contract
        true — None, or a positive finite float — so consumers need no guard
        of their own, which is why the fix is here and not at the two
        formatters that happened to divide.
        """
        if self.period is None or self.period <= 0:
            return None
        rate = 1.0 / self.period
        return rate if rate > 0 and rate != float("inf") else None

    def glyph(self, frames: str = HEARTBEAT_FRAMES) -> str:
        """The current frame. Identical across polls that brought nothing.

        `frames` is passed in by the renderer, which is the only part that
        knows what the output encoding can carry — see `heartbeat_frames()`.
        """
        return frames[self.beat % len(frames)]


class SessionHeartbeat:
    """Session-wide liveness, folded out of the source model's own delta.

    **It stops when the records stop, and says nothing else.** No idle label,
    no elapsed counter, no frame that turns on wall-clock time. A slow silent
    library therefore looks stopped — matplotlib spends 2.76 seconds rendering
    without emitting a record, and a frozen heartbeat is the truthful frame for
    that. It reads as "we cannot see anything", which is exactly right; the gap
    is the developer's to close by logging inside the loop, and making it
    visible is the value ladder working rather than a display failure to paper
    over.

    **No second poller.** `observe()` is handed the `SourceDelta`
    `RepeatingSourceModel` already fetched, because the whole point of the
    watermark is that a redraw costs what arrived rather than what the store
    holds — and a second delta query against the same store would double that
    cost to re-derive a number already in hand.

    The one extra read is a short `recent()` for the message, and only on a
    poll that brought records: a poll with nothing new cannot have a new last
    line. Its cost does not grow with the store — measured at 16µs for one row
    whether the store holds 10,000 or 1M, because `ORDER BY id DESC LIMIT n`
    walks the rowid backwards and stops. See `MESSAGE_LOOKBACK` for why it
    reads four rather than one, and for what that costs.

    `passthrough_level` is the renderer's, handed over rather than guessed:
    the heartbeat summarises the stream the display *collapsed*, and a record
    the display prints in full is not part of that.
    """

    def __init__(
        self, store: RecordStore, *, passthrough_level: int = PASSTHROUGH_LEVEL
    ) -> None:
        self._store = store
        self._passthrough_level = passthrough_level
        self._state = HeartbeatState()
        self._last_at: float | None = None
        self._period: float | None = None

    @property
    def state(self) -> HeartbeatState:
        """The heartbeat as of the last poll that brought records."""
        return self._state

    def observe(self, delta: SourceDelta) -> HeartbeatState:
        """Fold one poll's arrivals in. A poll that brought nothing is a no-op.

        Deliberately a no-op rather than an update with a zero: the state
        *is* the display, so leaving it untouched is what makes the row
        freeze. Anything written here on an empty poll — a decaying rate, an
        elapsed count, the next frame — would be the display moving on
        evidence it did not have.

        Task-event rows are not counted, because the store excludes them from
        the delta's counts: they have exact bars of their own, and a session
        running only instrumented tasks shows its liveness there rather than
        here — where it draws no row at all, which is a silence rather than a
        lie and is left as one. lumberjack: see issue #63

        Everything that arrived is counted, including records the display
        prints above the bars rather than collapsing. The count answers "is
        anything arriving at all", which a WARNING answers as well as a DEBUG
        does; only the *message* is restricted to what was collapsed.
        """
        arrived = sum(delta.counts.values())
        if arrived == 0:
            return self._state
        self._observe_period(arrived, delta)
        self._state = HeartbeatState(
            events=self._state.events + arrived,
            beat=self._state.beat + 1,
            period=self._period,
            message=self._newest_message(self._state.message),
        )
        return self._state

    def _observe_period(self, arrived: int, delta: SourceDelta) -> None:
        """Time the arrivals, via the same fold `_update_period` uses per source.

        Same two cases, over the whole delta rather than one source: a window
        running from the newest record we already knew about, or — on first
        sight, with nothing to measure back to — the delta's own span, which
        holds one fewer interval than it does records. See
        `smoothing.fold_interval` and `smoothing.advance_watermark` — the
        latter is why the watermark never moves backwards, which matters here
        because several workers can land in one delta.
        """
        newest = max(delta.last_at.values())
        previous = self._last_at
        self._last_at = advance_watermark(previous, newest)
        folded = fold_interval(previous, newest, min(delta.first_at.values()), arrived)
        if folded is None:
            return
        span, intervals = folded
        self._period = _smoothed(self._period, span / intervals)

    def _newest_message(self, previous: str | None) -> str | None:
        """The last thing the display swallowed, or what it swallowed before.

        Two kinds of record are skipped — the search walks back past them,
        `MESSAGE_LOOKBACK` rows at most, and the previous message stands if
        every one of them is skippable:

        * **A task event.** Its text is the tracking API's own (`task
          progress: reindex 40/100`), it is already on a named bar, and it is
          not in `events` either — so echoing it would have the row narrate a
          record it claims not to have seen.
        * **Anything at `passthrough_level` or above.** A WARNING prints above
          the bars in full, with its level and logger. Repeating it here says
          the same thing twice, and leaves a one-off warning sitting in the
          live row as though it were the program's current state.

        First line only. A multi-line message — a formatted table, a dumped
        payload — would otherwise push the bars down the screen and break the
        frame.
        """
        # `recent()` returns oldest-first, so the newest is the last one.
        for record in reversed(self._store.recent(n=MESSAGE_LOOKBACK)):
            if record.task_event is not None:
                continue
            if record.level_no >= self._passthrough_level:
                continue
            first_line = record.message.partition("\n")[0].strip()
            if first_line:
                return first_line
        return previous
