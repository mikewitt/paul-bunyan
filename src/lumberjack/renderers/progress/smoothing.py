"""The one thing every model here shares: how a new interval sample is folded in.

A source's period and the session's arrival rate are measured the same way and
for the same reason, so the weight and the fold live in one place rather than
being restated once per model. `fold_interval` and `advance_watermark` are the
other half of that: `RepeatingSourceModel._update_period` (one source) and
`SessionHeartbeat._observe_period` (every source, summed) reduce to the same
two cases — a window against the last thing we saw, or, on first sighting, the
delta's own span — so the arithmetic lives here once and each caller keeps
only its own state (a per-source dict versus a scalar).
"""

from __future__ import annotations

#: Weight given to the newest period sample. Low, because the estimate feeds
#: a number a human reads off a moving display: a rate that jitters every
#: redraw is harder to read than one that lags slightly.
PERIOD_SMOOTHING = 0.3


def _smoothed(known: float | None, sample: float) -> float:
    """Fold a new interval sample into an existing estimate.

    Low weight on the newest sample, because these estimates feed numbers a
    human reads off a moving display: a rate that jitters every redraw is
    harder to read than one that lags slightly.
    """
    if known is None:
        return sample
    return PERIOD_SMOOTHING * sample + (1 - PERIOD_SMOOTHING) * known


def fold_interval(
    previous: float | None, newest: float, oldest: float, count: int
) -> tuple[float, float] | None:
    """The `(span, intervals)` one delta of records contributes, or None.

    Two cases, both exact rather than approximate:

    * We have seen this source (or this session) before, so the window runs
      from the last record we already knew about to the newest in this delta,
      and `count` records fell inside it — one interval each.
    * First sighting, so the only window available is the delta's own span
      (`newest - oldest`), which contains `count - 1` intervals between its
      `count` records. A delta of one record establishes nothing.

    None covers every case with nothing to learn from: fewer than two records
    on a first sighting, or a span/interval count that is zero or negative —
    records sharing a timestamp (a burst inside one clock tick, or a coarse
    clock), which would otherwise report an infinite rate.
    """
    if previous is not None:
        span, intervals = newest - previous, count
    else:
        if count < 2:
            return None
        span, intervals = newest - oldest, count - 1
    if span <= 0 or intervals <= 0:
        return None
    return span, intervals


def advance_watermark(previous: float | None, newest: float) -> float:
    """The next high-water timestamp: `newest`, but never backwards.

    Several concurrent workers can land in one delta, so a later poll's
    newest timestamp can be *older* than an earlier poll's own newest —
    moving the mark back would inflate the next span with time already
    counted. `previous` is the watermark this reduces from, not the `newest`
    passed to `fold_interval` in the same call: both come from the same pair
    of readings, just used before and after the update.
    """
    return newest if previous is None else max(newest, previous)
