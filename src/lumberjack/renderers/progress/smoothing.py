"""The one thing every model here shares: how a new interval sample is folded in.

A source's period and the session's arrival rate are measured the same way and
for the same reason, so the weight and the fold live in one place rather than
being restated once per model.
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
