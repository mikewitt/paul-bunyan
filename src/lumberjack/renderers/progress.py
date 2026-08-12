"""Naive repeating-source detection behind the Phase 1 live bar.

Deliberately *not* `RepetitionAnalyzer` (Phase 4): no template extraction, no
masking, no clustering. "A repeating log shape" here means "records from the
same source location", which `count_by_source()` already groups for free — a
`logger.debug(...)` inside a loop hits the same line every iteration. Enough
to prove the premise: a log line that recurs is progress signal, not noise.

Counts come from the store, never from tallying the handler's live callback,
so every renderer reading that store sees the same numbers. Nothing here
imports `rich` — the model is display-independent.
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
    """Turns `store.count_by_source()` into an ordered list of bars."""

    def __init__(
        self,
        store: RecordStore,
        *,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        window_seconds: float | None = None,
    ) -> None:
        self._store = store
        self.min_repeats = min_repeats
        self.window_seconds = window_seconds
        # Insertion-ordered, so a source keeps the slot it was first given.
        # Unbounded: 300 repeating log sites means 300 bars.
        # lumberjack: see issue #8
        self._counts: dict[SourceKey, int] = {}

    def poll(self) -> list[BarState]:
        """Re-read the store and return the current bars.

        Once a source has a bar it keeps it, even if a `window_seconds` view
        later drops its count below the threshold — a bar that vanished
        mid-run would read as "this work stopped existing".
        """
        # Re-read wholesale rather than accumulated, so a count can fall after
        # an evict() or inside a window. lumberjack: see issue #9
        counts = self._store.count_by_source(self.window_seconds)
        # Newly-qualifying sources are added busiest-first; sources already
        # tracked keep their position, so bars never jump around on screen.
        fresh = sorted(
            (
                (source, count)
                for source, count in counts.items()
                if count >= self.min_repeats and source not in self._counts
            ),
            key=lambda item: (-item[1], item[0]),
        )
        for source, count in fresh:
            self._counts[source] = count
        for source in self._counts:
            if source in counts:
                self._counts[source] = counts[source]
        return self.bars()

    def bars(self) -> list[BarState]:
        """The most recent poll's bars, in display order. Empty before `poll()`."""
        return [
            BarState(source=source, count=count)
            for source, count in self._counts.items()
        ]
