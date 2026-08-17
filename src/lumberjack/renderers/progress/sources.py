"""Inferred bars: how fast a source repeats, what it runs inside, when it stopped.

The inferred half, and all of Phase 4b. Identity is the *source location*: a
`logger.debug(...)` inside a loop hits the same line every iteration, so
`(pathname, lineno, func_name)` groups a loop's ticks with no template
extraction, no masking and no clustering. On top of that grouping this module
measures each source's period, sorts sources by period to recover which loop
encloses which, takes the ratio between an enclosing loop and an enclosed one
as the inner loop's iteration count, and retires a bar whose source has gone
quiet.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from lumberjack.renderers.progress.heartbeat import HeartbeatState, SessionHeartbeat
from lumberjack.renderers.progress.smoothing import (
    _smoothed,
    advance_watermark,
    fold_interval,
)
from lumberjack.schema import SourceKey
from lumberjack.store import WorkerKey

if TYPE_CHECKING:
    from collections.abc import Callable

    from lumberjack.store import RecordStore, SourceDelta

#: How many times a source location must have logged before it earns a bar.
#: Low on purpose: two hits is a coincidence, three is a loop.
DEFAULT_MIN_REPEATS = 3

#: Seconds between store polls (and therefore between redraws). Timer-driven
#: and deliberately independent of log volume — a million records a second
#: must still cost five redraws a second.
DEFAULT_REFRESH_INTERVAL = 0.2

#: How close two sources' periods must be to count as the same loop body.
#: `logger.debug` on line 6 and line 12 of one loop fire once each per
#: iteration, so their periods match to within scheduling noise.
SAME_LOOP_TOLERANCE = 0.15


def _same_loop_period(one: float, other: float) -> bool:
    """Whether two periods are close enough to be two lines in one body.

    The same tolerance `_levels()` cuts levels on below, and for the same
    reason: `logger.debug` on line 6 and line 12 of one loop fire once each
    per iteration, so their periods match to within scheduling noise. Shared
    with `loops.LoopRowModel`'s runtime-fallback grouping, which asks the
    identical question about two sources it cannot place with the AST.
    """
    slower, faster = max(one, other), min(one, other)
    return slower > 0 and faster >= slower * (1 - SAME_LOOP_TOLERANCE)


#: The smallest period ratio that means nesting rather than noise. Below 2 an
#: "inner loop" would run fewer than two iterations per outer one, which no
#: bar can usefully show and which jitter alone can manufacture.
MIN_NESTING_RATIO = 2.0

#: How far a re-measured ratio may move and still count as the same
#: observation.
#:
#: This and the confirmation count below are one dial, not two. `PERIOD_SMOOTHING`
#: caps how far a period estimate can move in a single poll, so a lurching loop
#: still yields a ratio that only drifts — which means a single in-tolerance
#: poll proves very little, and the discrimination has to come from demanding
#: several in a row.
RATIO_TOLERANCE = 0.15

#: Consecutive polls a parent/ratio pairing must survive before it is believed,
#: which guards against a poll catching a loop mid-spin-up, when a period
#: estimate built from three records means very little. The tolerance above is
#: what rejects an unstable ratio; this only has to outlast the transient, and
#: each confirmation costs a refresh interval of pulsing before the bar
#: promotes.
CONTAINMENT_CONFIRMATIONS = 2

#: Silences worth this many of a source's own periods retire its bar. There is
#: no completion signal — nothing raises `StopIteration` at a log line — so a
#: slow iteration and a finished loop look identical, and this is the 80%
#: answer rather than a correct one.
IDLE_PERIODS = 10.0

#: Floor under the idle threshold. A loop iterating every millisecond has a
#: 10-period threshold of 10ms, which is shorter than the pipeline that feeds
#: this model: records wait up to one flush interval in the buffer and up to
#: one refresh interval for the poll. Without the floor a fast loop would
#: retire and resurrect on alternate frames.
MIN_IDLE_SECONDS = 1.0


@dataclasses.dataclass(frozen=True, slots=True)
class BarState:
    """One bar's worth of state: which source it tracks and how far it has got.

    `count` is cumulative and monotonic — the honest tally of records this
    source has produced. `total` and `cycle_current` are the *inferred* pair,
    and they describe one iteration of the enclosing loop rather than the run:
    a bar nested inside another fills up, resets, and fills again. Both are
    absent until containment analysis has something to say, which is the
    normal state for an outermost loop — nothing bounds it, so it pulses
    forever, and that is the correct rendering rather than a missing feature.
    """

    source: SourceKey
    count: int
    #: Seconds between records from this source, smoothed. None until two
    #: records have been seen — one record establishes no interval.
    period: float | None = None
    #: `created` of the newest record from this source, or None before the
    #: first. Reported because it is what says *which* of a loop body's call
    #: sites fired last, and therefore where the iteration has got to — see
    #: `position.CyclePositionModel`. It comes out of the same aggregate the
    #: period does, so nothing new is queried to expose it.
    last_at: float | None = None
    #: The source this one was inferred to run inside, if any.
    parent: SourceKey | None = None
    #: Iterations per enclosing cycle, inferred. None while unknown.
    total: int | None = None
    #: Iterations so far within the current enclosing cycle.
    cycle_current: int = 0
    #: How deep the inferred containment chain runs. 0 for an outermost loop.
    depth: int = 0
    #: Nothing has arrived for `IDLE_PERIODS` of this source's own period.
    idle: bool = False
    #: Every concurrent worker this line has been seen on, accumulated over
    #: the run. Reported rather than kept private because it is the same fact
    #: containment scoping needs and the same fact display-side grouping needs
    #: — two sources cannot be one loop body if no worker ever ran both.
    workers: frozenset[WorkerKey] = frozenset()


class RepeatingSourceModel:
    """Accumulates `store.count_by_source_since()` into an ordered list of bars.

    Counting forward from a watermark rather than re-tallying the store is
    what keeps a redraw affordable — the query costs what arrived since the
    last poll, not what the store holds. It also makes the totals monotonic
    by construction, which is what a progress bar means: `evict()` can drop
    the rows a bar counted without the bar counting backwards.
    """

    def __init__(
        self,
        store: RecordStore,
        *,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        clock: Callable[[], float] = time.time,
        heartbeat: SessionHeartbeat | None = None,
    ) -> None:
        self._store = store
        self.min_repeats = min_repeats
        # Fed from this model's poll rather than polling for itself: the
        # heartbeat is a sum over the same delta already being fetched, and
        # fetching it twice would double the one query a redraw is supposed to
        # cost. Injectable because *what* it echoes is the display's business
        # — the renderer builds one that knows its own passthrough level —
        # while *when* it is fed is this model's.
        self._heartbeat = (
            heartbeat if heartbeat is not None else SessionHeartbeat(store)
        )
        # Injectable so idle retirement can be driven deterministically. Every
        # other timestamp here comes from `LogRecord.created`, which is
        # `time.time()`, so the default is the same clock the records used.
        self._clock = clock
        # Every source seen, including those still short of min_repeats —
        # their running total is what lets them qualify later. Unbounded, and
        # that is the identity layer working as intended: 300 repeating log
        # sites are 300 things that were captured. How many *rows* they become
        # is `loops.LoopRowModel`'s question, and its answer is a handful.
        self._totals: dict[SourceKey, int] = {}
        # Arrival order, append-only. Where a row is drawn is decided from
        # structure downstream (`layout.depth_first_order`); this is only the
        # order sources qualified in, which breaks the ties that leaves.
        # The set mirrors it purely for membership: this is checked once per
        # source per poll, and a list scan there would be quadratic.
        self._shown: list[SourceKey] = []
        self._shown_set: set[SourceKey] = set()
        # Newest `created` seen per source, and the smoothed interval between
        # records. A source's own recurrence interval is its loop's period —
        # no clustering needed to time a loop, only to decide how many bars
        # to draw (#38).
        self._last_at: dict[SourceKey, float] = {}
        self._period: dict[SourceKey, float] = {}
        # Inferred containment, frozen once believed. `_parent`/`_total` are
        # written exactly once per source, because a bar that re-parents or
        # re-scales as the estimate wobbles is unreadable — the same reason
        # bars never move. `_candidate` holds the pairing still on probation.
        self._parent: dict[SourceKey, SourceKey] = {}
        self._total: dict[SourceKey, int] = {}
        self._candidate: dict[SourceKey, tuple[SourceKey, float, int]] = {}
        # Cumulative count as of the enclosing loop's last observed iteration,
        # so `count - base` is the position within the current cycle.
        self._cycle_base: dict[SourceKey, int] = {}
        # Every worker each source has been seen on, accumulated rather than
        # per-delta: a thread that logged a line once still ran that line.
        self._workers: dict[SourceKey, set[WorkerKey]] = {}
        self._watermark = 0

    def poll(self) -> list[BarState]:
        """Fold in whatever arrived since the last call and return the bars.

        Once a source has a bar it keeps it: a bar that vanished mid-run
        would read as "this work stopped existing". A bar that goes quiet is
        marked idle instead, which says the loop stopped without claiming its
        history never happened.
        """
        delta = self._store.count_by_source_since(self._watermark)
        self._watermark = delta.last_id
        self._heartbeat.observe(delta)
        for source, count in delta.counts.items():
            self._totals[source] = self._totals.get(source, 0) + count
            self._update_period(source, count, delta)
            self._workers.setdefault(source, set()).update(delta.workers[source])
        # Only a source that just gained records can newly cross the
        # threshold — every source already over it was promoted on the poll
        # that took it there — so this scans the delta rather than every
        # source ever seen. Newly-qualifying ones join busiest-first; those
        # already shown keep the slot they were first given.
        fresh = sorted(
            (
                (source, self._totals[source])
                for source in delta.counts
                if self._totals[source] >= self.min_repeats
                and source not in self._shown_set
            ),
            key=lambda item: (-item[1], item[0]),
        )
        for source, _ in fresh:
            self._shown.append(source)
            self._shown_set.add(source)
        self._infer_containment(delta)
        self._advance_cycles(delta)
        return self.bars()

    @property
    def heartbeat(self) -> HeartbeatState:
        """Session-level liveness as of the last poll. See `SessionHeartbeat`.

        Read through this model because that is where the delta is. A caller
        wanting the heartbeat wants the bars in the same breath, and both come
        out of one `poll()`.
        """
        return self._heartbeat.state

    def _update_period(self, source: SourceKey, count: int, delta: SourceDelta) -> None:
        """Fold this delta's timing into the source's interval estimate.

        See `smoothing.fold_interval` for the two cases (seen before / first
        sighting) and `smoothing.advance_watermark` for why the stored
        timestamp only ever moves forward: several workers can land in one
        delta, so a later poll's newest record for this source can be older
        than an earlier poll's, and writing it unguarded would inflate the
        next span with time already counted.

        Smoothed rather than replaced, because a single slow iteration should
        not make the displayed rate lurch.
        """
        # `last_at` and `first_at` carry the same keys as `counts` by
        # construction, so a source in the delta always has timestamps here.
        last_at = delta.last_at[source]
        previous = self._last_at.get(source)
        self._last_at[source] = advance_watermark(previous, last_at)
        folded = fold_interval(previous, last_at, delta.first_at[source], count)
        if folded is None:
            return
        span, intervals = folded
        self._period[source] = _smoothed(self._period.get(source), span / intervals)

    def _levels(self) -> list[list[SourceKey]]:
        """Drawn sources grouped into loop levels, outermost first.

        A source's own recurrence interval is its loop's period, so two
        sources with the *same* period are two log lines in one loop body —
        the 1:1 case — and a source with a much shorter period is running
        inside a slower one. Sorting by period descending and cutting wherever
        the period drops by more than `SAME_LOOP_TOLERANCE` recovers that
        structure directly, with no pairwise comparison and nothing to scale
        badly as source count grows.

        Ordering by period alone is the cheap 80% of #38's ratio rule: it does
        not verify interleaving, so two *unrelated* loops whose rates happen to
        sit in a stable integer ratio will be read as nested. The cost of being
        wrong is an indent and a total that the count outruns, at which point
        the bar goes back to pulsing — cosmetic, per Principle 10.

        Sorted by source key within a level so the walk is deterministic
        across polls; two sources with equal periods must not swap parents
        because a dict iterated differently.

        Drawn sources only, which has a consequence worth knowing: an outer
        loop cannot parent anything until it has itself iterated
        `min_repeats` times, so a nested bar pulses through the first few
        outer iterations even where the inner loop settled immediately. That
        is the right way round — an enclosing loop seen twice has not shown
        it is a loop.
        """
        timed = sorted(
            (
                (self._period[source], source)
                for source in self._shown
                if source in self._period and self._period[source] > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        levels: list[list[SourceKey]] = []
        representative = 0.0
        for period, source in timed:
            if levels and _same_loop_period(representative, period):
                levels[-1].append(source)
            else:
                levels.append([source])
                representative = period
        return levels

    def _infer_containment(self, delta: SourceDelta) -> None:
        """Promote a stable parent/ratio pairing into a believed one.

        The ratio between a level's period and its enclosing level's *is* the
        inner loop's iteration count: if the outer line fires every 8 seconds
        and the inner every second, the inner runs 8 times per outer
        iteration. Containment and the total come from one measurement.

        Nothing is believed on first sight. A pairing has to survive
        `CONTAINMENT_CONFIRMATIONS` polls *that brought new records for the
        child* — which is the only kind that re-measures anything. Counting
        every poll instead would confirm a pairing on the next redraw whether
        or not a single record arrived, since periods move only when records
        do: the guard would be a 200ms timer wearing the costume of a second
        opinion. Once believed it is frozen: confidence only increases, and a
        bar that re-parents mid-run is worse than one that never claimed.
        """
        levels = self._levels()
        for depth, level in enumerate(levels[1:], start=1):
            for source in level:
                if source in self._parent:
                    continue  # frozen; confidence only increases
                if source not in delta.counts:
                    # No new records, so no new measurement. Leave any
                    # candidate standing rather than resetting it: a quiet
                    # poll is not evidence against the pairing either.
                    continue
                parent = self._enclosing(source, levels[:depth])
                if parent is None:
                    self._candidate.pop(source, None)
                    continue
                ratio = self._period[parent] / self._period[source]
                if ratio < MIN_NESTING_RATIO:
                    self._candidate.pop(source, None)
                    continue
                seen = self._candidate.get(source)
                if (
                    seen is not None
                    and seen[0] == parent
                    and abs(ratio - seen[1]) <= RATIO_TOLERANCE * seen[1]
                ):
                    confirmations = seen[2] + 1
                else:
                    confirmations = 1
                if confirmations >= CONTAINMENT_CONFIRMATIONS:
                    self._parent[source] = parent
                    self._total[source] = round(ratio)
                    self._candidate.pop(source, None)
                    # The cycle starts now rather than at the run's first
                    # record: the count so far spans however many enclosing
                    # iterations already went by, and charging those to the
                    # first drawn cycle would show an instant overrun.
                    self._cycle_base[source] = self._totals[source]
                else:
                    self._candidate[source] = (parent, ratio, confirmations)

    def _enclosing(
        self, source: SourceKey, outer: list[list[SourceKey]]
    ) -> SourceKey | None:
        """The nearest slower source that could actually be enclosing this one.

        "Could actually be" is the whole of the check, and it is a worker
        comparison: a loop cannot contain a loop running on another thread,
        another process, or another asyncio task, however neatly their rates
        happen to divide. Without it any two unrelated workers whose periods
        sit in a stable integer ratio get read as nested — which is not a
        hypothetical, it is what three independent worker threads at 4ms,
        10ms and 100ms do.

        Searched outward rather than taken from the immediately enclosing
        level, because an unrelated source on another worker can easily land
        between a real parent and its child in a list ordered purely by
        period. Levels are scanned nearest-first so the tightest enclosing
        loop wins, and within a level the first member does, which
        `_levels()` keeps stable across polls.
        """
        mine = self._workers.get(source, set())
        for level in reversed(outer):
            for candidate in level:
                if mine & self._workers.get(candidate, set()):
                    return candidate
        return None

    def _advance_cycles(self, delta: SourceDelta) -> None:
        """Rebase each nested bar when its enclosing loop iterated.

        The enclosing loop leaves no marker beyond its own records, so "a new
        cycle started" means "the parent logged again", which the delta says
        directly. What it does not say is *where* among the child's records
        that happened, only that both fell in the same window — so the child's
        records are split across the boundary by assuming they are evenly
        spread through their own span, which for a loop is very nearly true.

        The child's own first/last timestamps drive that, not its period: the
        period is a smoothed estimate that lags a loop changing pace, while
        the span is measured inside this window and cannot be stale.
        """
        for source, parent in self._parent.items():
            arrived = delta.counts.get(source, 0)
            if parent not in delta.counts:
                continue  # the enclosing loop did not iterate
            boundary = delta.last_at[parent]
            if arrived == 0:
                carried = 0
            else:
                first_at, last_at = delta.first_at[source], delta.last_at[source]
                span = last_at - first_at
                fraction = 1.0 if span <= 0 else (last_at - boundary) / span
                carried = round(arrived * min(1.0, max(0.0, fraction)))
                if last_at < boundary:
                    # Every record in this window predates the boundary, and
                    # a zero span above would otherwise have carried them all.
                    carried = 0
            self._cycle_base[source] = self._totals[source] - carried

    def bars(self) -> list[BarState]:
        """The most recent poll's bars, in arrival order. Empty before `poll()`.

        Arrival order — first-qualified — and not display order. The two used
        to be the same thing, which is what made a nested row indent under
        whichever unrelated loop happened to precede it. Laying rows out by
        containment is `layout.depth_first_order()`'s job, and grouping call
        sites into the loops a person actually wants to see is
        `loops.LoopRowModel`'s; this list is one entry per *source location*,
        which is the identity layer and stays exactly that.
        """
        now = self._clock()
        return [
            BarState(
                source=source,
                count=self._totals[source],
                period=self._period.get(source),
                last_at=self._last_at.get(source),
                parent=self._parent.get(source),
                total=self._total.get(source),
                cycle_current=max(
                    0, self._totals[source] - self._cycle_base.get(source, 0)
                ),
                depth=self._depth(source),
                idle=self._is_idle(source, now),
                workers=frozenset(self._workers.get(source, ())),
            )
            for source in self._shown
        ]

    def _depth(self, source: SourceKey) -> int:
        """How deep the inferred containment chain runs above this source.

        Walked rather than stored for the same reason `TaskProgressModel` does
        it: the chain is shallow, the walk is a dict lookup per level, and
        storing it would mean recomputing every descendant whenever one
        pairing freezes.

        The `seen` guard is load-bearing against real data, not just against a
        hypothetical bug. `_levels()` orders strictly by period, so no single
        poll can produce a cycle — but a frozen pairing is *historical*, and
        periods keep moving. Freeze A as B's parent, let A slow down until its
        period falls below B's, and A becomes an eligible child of B on a later
        poll. Without the guard that pair walks forever and hangs the render
        thread; with it the cost is two bars indented oddly, which is the
        cheaper wrong answer.
        """
        depth = 0
        seen = {source}
        parent = self._parent.get(source)
        while parent is not None and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = self._parent.get(parent)
        return depth

    def _is_idle(self, source: SourceKey, now: float) -> bool:
        """Whether this source has been quiet long enough to call it finished.

        Untimed sources are never idle: without a period there is no scale to
        judge the silence against, and a bar that has only ever seen three
        records should not be retired for being slow.
        """
        period = self._period.get(source)
        last_at = self._last_at.get(source)
        if period is None or last_at is None:
            return False
        return now - last_at > max(IDLE_PERIODS * period, MIN_IDLE_SECONDS)
