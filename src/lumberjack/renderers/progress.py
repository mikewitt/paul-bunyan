"""Naive repeating-source detection behind the Phase 1 live bar.

Deliberately *not* `RepetitionAnalyzer` (Phase 4): no template extraction, no
masking, no clustering. "A repeating log shape" here means "records from the
same source location", which `count_by_source()` already groups for free — a
`logger.debug(...)` inside a loop hits the same line every iteration. Enough
to prove the premise: a log line that recurs is progress signal, not noise.

Counts come from the store, never from tallying the handler's live callback,
so every renderer reading that store sees the same numbers. Nothing here
imports `rich` — the model is display-independent.

The store is read forward from a watermark rather than re-tallied, so a
redraw costs what arrived since the last one instead of what the store
holds.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING

from lumberjack.schema import SourceKey

if TYPE_CHECKING:
    from lumberjack.store import RecordStore

#: How many times a source location must have logged before it earns a bar.
#: Low on purpose: two hits is a coincidence, three is a loop.
DEFAULT_MIN_REPEATS = 3

#: Seconds between store polls (and therefore between redraws). Timer-driven
#: and deliberately independent of log volume — a million records a second
#: must still cost five redraws a second.
DEFAULT_REFRESH_INTERVAL = 0.2


@dataclasses.dataclass(frozen=True, slots=True)
class BarState:
    """One bar's worth of state: which source it tracks and how far it has got.

    There is no `total`: this proof knows how many records have arrived, never
    how many are still coming. Exact totals are the tracking API's job (Phase 2).
    """

    source: SourceKey
    count: int

    @property
    def label(self) -> str:
        """Human-readable source location, e.g. `worker.py:42 process()`."""
        name = os.path.basename(self.source.pathname)
        return f"{name}:{self.source.lineno} {self.source.func_name}()"


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
    ) -> None:
        self._store = store
        self.min_repeats = min_repeats
        # Every source seen, including those still short of min_repeats —
        # their running total is what lets them qualify later.
        # Unbounded: 300 repeating log sites means 300 bars.
        # lumberjack: see issue #8
        self._totals: dict[SourceKey, int] = {}
        # Display order, append-only, so bars never jump around on screen.
        # The set mirrors it purely for membership: this is checked once per
        # source per poll, and a list scan there would be quadratic.
        self._shown: list[SourceKey] = []
        self._shown_set: set[SourceKey] = set()
        self._watermark = 0

    def poll(self) -> list[BarState]:
        """Fold in whatever arrived since the last call and return the bars.

        Once a source has a bar it keeps it: a bar that vanished mid-run
        would read as "this work stopped existing".
        """
        delta = self._store.count_by_source_since(self._watermark)
        self._watermark = delta.last_id
        for source, count in delta.counts.items():
            self._totals[source] = self._totals.get(source, 0) + count
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
        return self.bars()

    def bars(self) -> list[BarState]:
        """The most recent poll's bars, in display order. Empty before `poll()`."""
        return [
            BarState(source=source, count=self._totals[source])
            for source in self._shown
        ]
