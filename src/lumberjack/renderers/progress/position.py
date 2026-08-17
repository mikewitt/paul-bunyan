"""Position within the current iteration: the second row, and when it is earned.

**The criterion is legibility, and it is the whole rule.** A row earns its
place by updating at a rate a human can read. `examples/demo.py sequence` is
one loop whose body narrates five stages over about a second and a half, and
the loop row alone ticks once per iteration — correct, and useless, because it
cannot distinguish a running program from a hung one, which is the first
question the display exists to answer. `siblings` has the identical structure
at 120 iterations a second, where the loop row is plainly alive and a
sub-iteration bar would be a blur nobody can read. Same merge, same shape,
different number of rows, decided by measured period rather than by taste.

**The position itself is read, not tracked.** `static.CallSite.position` and
`body_size` are the 1-based ordinal of a call site within its innermost loop
body, in textual order, and the size of that body — computed by one `ast` walk
and frozen on the `CallSite`. So a record arrives from line 171 and its
position is a dict lookup: `3 of 5`. Nothing is counted at runtime, no sequence
column is read, and no per-record query is added to the poll.

That last part is the point. Interleaving analysis — recovering a body's
canonical order by counting how many B's fall between consecutive A's — was
designed twice and refused twice, both times on cost: it needs a per-record
sequence read where everything else in the analysis needs only the aggregate
`count_by_source_since()` already returns. Static structure supplies the order
outright, so the expensive half of this feature evaporated rather than being
paid for.

**Which stage is current comes from the aggregate too.** `BarState.last_at` is
the newest `created` this source has produced, which the delta reports per
source per poll. The member with the greatest one fired most recently, and its
ordinal is where the iteration has got to. A loop slow enough to earn this row
emits at most a handful of records per redraw, so "most recent" is exact rather
than a sample.

Three ways it declines to draw, and each is a refusal rather than a guess:

* **A body with a branch has no stable order.** `Loop.stable_order` is False as
  soon as one call site sits under an `if`, `try` or `match`, and a determinate
  bar built on it would show a *wrong* percentage rather than an imprecise one
  — `transform` in `pipeline` is exactly this, its conditional warning making
  the body's sequence depend on the data. Principle 10 licenses an approximate
  bar, never a fabricated one.
* **A body with one call site has nothing to say.** `1 of 1` on every record is
  a row that never moves, which fails the criterion it was admitted under.
  `phases`' once-per-stage announcement line is that case.
* **No source on disk, or a template that failed the drift guard.** Then there
  is no ordinal to look up. The loop row is unaffected and draws exactly as it
  did before, which is what the bare-install and generated-code paths need.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from lumberjack import static

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from lumberjack.renderers.progress.sources import BarState
    from lumberjack.schema import SourceKey

#: Seconds per iteration at or above which the loop row alone is too slow to
#: read, and the body's stages become worth a row of their own.
#:
#: One second, and the two neighbouring numbers are what argue for it. A
#: redraw is every 200ms (`DEFAULT_REFRESH_INTERVAL`), so a loop at this
#: threshold changes the loop row once every five frames — about the slowest
#: tick a person still reads as motion rather than as a stopped display, and
#: the point past which "is this thing alive?" stops being answered promptly by
#: the row that exists. Below it the loop row is visibly moving on its own and
#: the finer row is worse than nothing: `siblings` iterates every 8ms, so a
#: position bar there would repaint from whichever of ~24 iterations the poll
#: happened to land in, showing noise at four frames a second.
#:
#: Not 2s, because `sequence` — the shape this was built for — iterates every
#: 1.5s, and a threshold above it would refuse the motivating case. Not 0.5s,
#: because at two ticks a second the loop row already answers the liveness
#: question and the second row is only clutter. Anywhere in 1–2s is defensible;
#: the bottom of that range is chosen so the feature fires where it is wanted
#: and the tie is broken toward showing more.
MIN_LEGIBLE_PERIOD = 1.0

#: Call sites a loop body needs before its ordinal position means anything.
#: One site is `1 of 1` forever, which is a row that cannot move — it would
#: fail the very criterion that admitted it.
MIN_BODY_SITES = 2


@dataclasses.dataclass(frozen=True, slots=True)
class CyclePosition:
    """How far through its body one iteration of a loop has got.

    `current` and `total` are the AST's, not a measurement: the ordinal of the
    call site that fired most recently and how many the body holds. They reset
    on every iteration by construction, because the ordinal does.
    """

    current: int
    total: int
    #: The firing call site's template, described for display — `batch …:
    #: validating checksums`. It names the *stage*, which is what changes as
    #: the bar fills, and it is the same structured field a loop row is named
    #: from rather than anything parsed out of rendered text.
    label: str
    #: Which member fired. The identity underneath the stage name.
    source: SourceKey


class CyclePositionModel:
    """Whether a loop row has earned a position row, and where it points.

    Owns two frozen decisions and nothing else. Both are frozen for the same
    reason every other decision in this layer is: a row that appears and
    disappears as a measurement wobbles across a threshold is worse than one
    that never appeared.

    * **Admission is one-way.** A loop whose period sits near
      `MIN_LEGIBLE_PERIOD` must not gain and lose a row every poll, so the
      criterion is asked until it says yes and never again after. A loop that
      later speeds up keeps a row it no longer needs, which is the cheaper
      wrong answer — the alternative is a row vanishing mid-run, which reads
      as work that stopped existing.
    * **Whether the body is orderable is asked once per row.** It is a property
      of one file at one moment, and the group it is asked about is itself
      frozen, so re-reading it every poll would be an `os.stat` per row per
      redraw to re-answer a question whose inputs cannot move.
    """

    def __init__(self, *, min_period: float = MIN_LEGIBLE_PERIOD) -> None:
        self._min_period = min_period
        self._earned: set[SourceKey] = set()
        self._orderable: dict[SourceKey, bool] = {}

    def of(
        self,
        key: SourceKey,
        *,
        loop: SourceKey | None,
        period: float | None,
        sites: Mapping[SourceKey, static.CallSite | None],
        states: Sequence[BarState],
        label_of: Callable[[SourceKey], str],
    ) -> CyclePosition | None:
        """This row's position within its current iteration, or None.

        `loop` is the `for`/`while` statement the AST grouped this row on, and
        None for a row grouped by the runtime fallback — which has no body
        order to read and therefore never draws one of these.
        """
        if key not in self._earned:
            if not self._admits(key, loop, period, states, sites):
                return None
            self._earned.add(key)
        return self._where(states, sites, label_of)

    def _admits(
        self,
        key: SourceKey,
        loop: SourceKey | None,
        period: float | None,
        states: Sequence[BarState],
        sites: Mapping[SourceKey, static.CallSite | None],
    ) -> bool:
        """Whether this row is both too slow to read and finely enough narrated."""
        if loop is None or period is None or period < self._min_period:
            return False
        if not self._is_orderable(key, loop):
            return False
        # An admitted row must be able to answer *now*, not eventually: nothing
        # withdraws admission, so granting it before any member has a position
        # would promise a row that might never have a number in it.
        return next(_stages(states, sites), None) is not None

    def _is_orderable(self, key: SourceKey, loop: SourceKey) -> bool:
        cached = self._orderable.get(key)
        if cached is None:
            self._orderable[key] = cached = _orderable(loop)
        return cached

    def _where(
        self,
        states: Sequence[BarState],
        sites: Mapping[SourceKey, static.CallSite | None],
        label_of: Callable[[SourceKey], str],
    ) -> CyclePosition | None:
        """The most recently fired member's ordinal, as a position.

        Ties break on the source key rather than arbitrarily, so two call sites
        sharing a timestamp — a burst inside one clock tick — do not make the
        row flip between them on alternate polls.
        """
        stages = list(_stages(states, sites))
        if not stages:  # pragma: no cover - admission already required one
            return None
        _, source, current, total = max(stages, key=lambda stage: stage[:2])
        return CyclePosition(
            current=current,
            total=total,
            label=label_of(source),
            source=source,
        )


def _stages(
    states: Sequence[BarState],
    sites: Mapping[SourceKey, static.CallSite | None],
) -> Iterator[tuple[float, SourceKey, int, int]]:
    """`(newest timestamp, source, ordinal, body size)` per member the AST placed.

    Resolved here rather than handing back the `CallSite`, so the two Optional
    fields are narrowed once at the point that knows they travel together:
    `position` and `body_size` are both non-None exactly when the call site is
    inside a loop body.

    A member with no site never appears: a source whose template failed the
    drift guard cannot join a statically grouped row in the first place, so in
    practice this only skips one that has not been timed yet.
    """
    for state in states:
        site = sites.get(state.source)
        if site is None or site.position is None or site.body_size is None:
            continue
        if state.last_at is None:
            continue
        yield state.last_at, state.source, site.position, site.body_size


def _orderable(loop: SourceKey) -> bool:
    """Whether this loop's body has an order worth drawing a bar against.

    Reads the same `Loop` the grouping was taken from, so the file has already
    passed `template_matches()` for the member that founded the row — the drift
    guard is upstream of here, not repeated in it.
    """
    structure = static.analyze_file(loop.pathname)
    if structure is None:  # pragma: no cover - the group came from this file
        return False
    found = structure.loops.get(loop.lineno)
    if found is None:  # pragma: no cover - likewise
        return False
    return found.stable_order and len(found.call_sites) >= MIN_BODY_SITES
