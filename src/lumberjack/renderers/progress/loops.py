"""Loop rows: one row per loop a person would recognise, not per call site.

**Display unit is not identity unit, and conflating them was the root
mistake.** Source location is the right *identity* key — exact, cheap, no
inference — and nothing here moves off it. But it became the *display* unit by
default, because grouping already produced it. A person does not want a bar per
`logger.debug`; they want the shape of their program. `examples/demo.py
siblings` is the smallest case: one loop, four call sites in its body, four
identical rows at 121/s, and 1600 where the answer is 400.

So this module groups the source-location bars `RepeatingSourceModel` already
computes into one row per inferred loop. It is **rendering-side grouping, not
new inference** — no new store query, no new signal, nothing reaching into the
model's frozen state. That separation is deliberate: `BarState`'s append-only
`_shown`, its write-once `_parent`/`_total`/`_cycle_base` and its monotonic
counts are what make eviction safe, and making them loop-scoped instead of
source-scoped would break that guarantee to improve a label.

Two sources of grouping, in that order:

* **Static, and exact.** `lumberjack.static` reads the file and says outright
  which call sites share an innermost loop, which loop encloses which, and what
  each line's template is. Everything it says is gated on
  `static.template_matches()` — a file edited since the running process
  imported it makes `file:lineno` point somewhere else, and every structural
  claim keyed on it wrong.
* **Runtime, and approximate.** No source on disk (`exec`, a generated module,
  a frozen importer), or a template that did not survive to runtime: fall back
  to what shipped before — equal periods and a shared worker. Principle 9,
  applied to a data source rather than a package: degrade, never error.

What static also buys is a **veto**. Period ordering cannot tell "A encloses B"
from "A precedes B", and in `phases` it reads the once-per-stage announcement
line as an enclosing loop and hands the running stage a total of 49 for a loop
that runs 40 times. Where the AST shows the child is a top-level loop in a
different function, the claimed parent cannot lexically enclose it, so the
total is withheld and the row pulses. The indent stays — the stage function
really is called from inside that loop — and only the fabricated number goes.
Static analysis fails *silently* on cross-function containment rather than
denying it, so the veto is written the conservative way round: it fires when
the claimed parent's loop is simply absent from the child's static chain,
whatever nesting depth the child sits at.
"""

from __future__ import annotations

import dataclasses
import ntpath
import time
from typing import TYPE_CHECKING, NamedTuple

from lumberjack import static
from lumberjack.renderers.progress.layout import depth_first_order
from lumberjack.renderers.progress.position import CyclePosition, CyclePositionModel
from lumberjack.renderers.progress.sources import (
    DEFAULT_MIN_REPEATS,
    BarState,
    RepeatingSourceModel,
    _same_loop_period,
)
from lumberjack.renderers.progress.templates import TemplateIndex, describe_template
from lumberjack.schema import SourceKey

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from lumberjack.renderers.progress.heartbeat import HeartbeatState, SessionHeartbeat
    from lumberjack.store import RecordStore


class _GroupId(NamedTuple):
    """What decides that two call sites are one row.

    `kind` is which evidence produced it, and it is part of the identity on
    purpose: a statically-grouped loop and a runtime-grouped one must never
    merge, because the runtime one is a guess and joining it to an exact group
    would launder the guess into the exact answer.
    """

    #: "loop" — a loop body the AST identified, `at` being the loop statement.
    #: "site" — one call site standing alone, `at` being the site itself.
    kind: str
    at: SourceKey


@dataclasses.dataclass(frozen=True, slots=True)
class LoopRow:
    """One loop, as the display draws it.

    `count` is **iterations of this loop**, not records captured — the number
    `siblings` should read is 400, not the 1600 log calls behind it. Every call
    site there says `row %d`: the row is what the code is making progress
    through, and how many lines the author chose to narrate each one with is
    not something anybody asked about. Record count is an identity-layer
    number, and the store and the exit summary are where it is asked for.

    `total` and `cycle_current` describe one iteration of the *enclosing* loop
    rather than the run, exactly as `BarState`'s do, and are absent until
    containment analysis has something to say — the normal state of an
    outermost loop, which nothing bounds.
    """

    #: Stable identity: the first member source to qualify for a bar, and never
    #: migrated afterwards. A row whose key moved would be a new rich `Task`,
    #: which silently resets its elapsed clock.
    key: SourceKey
    #: Every source location folded into this row, in the order they qualified.
    members: tuple[SourceKey, ...]
    label: str
    #: The member driving the row's numbers: the one that fired most often.
    clock: SourceKey
    count: int
    period: float | None = None
    #: The `key` of the row this one was inferred to run inside.
    parent: SourceKey | None = None
    total: int | None = None
    cycle_current: int = 0
    depth: int = 0
    idle: bool = False
    #: Where this iteration has got to within the loop's body, when the loop
    #: ticks too slowly for the row above to answer "is it still running?".
    #: None whenever it does not — which is most loops. A second, determinate
    #: row drawn under this one; see `position.CyclePositionModel`.
    position: CyclePosition | None = None

    @property
    def rate(self) -> float | None:
        """Iterations per second, or None while the period is not a usable number.

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

    @property
    def is_determinate(self) -> bool:
        """Whether the bar may claim a percentage. See `BarState`."""
        return self.total is not None and self.cycle_current <= self.total


class LoopRowModel:
    """`RepeatingSourceModel`'s per-source bars, grouped into per-loop rows.

    Owns the source model rather than subclassing it, because the two answer
    different questions and both answers are wanted: `bars()` is the identity
    layer — one entry per source location, what was captured — and `rows()` is
    the display layer. The exit summary and the store speak the first language;
    the screen speaks the second.

    **Every grouping decision is frozen the moment it is made.** A source is
    assigned to a row once and never moves, a row's canonical key never
    migrates, and membership only ever grows. Re-grouping would mean a drawn
    row disappearing, which is the thing the never-move rule was always really
    protecting — and it would take the row's elapsed clock with it.
    """

    def __init__(
        self,
        store: RecordStore,
        *,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        clock: Callable[[], float] = time.time,
        heartbeat: SessionHeartbeat | None = None,
    ) -> None:
        self._sources = RepeatingSourceModel(
            store, min_repeats=min_repeats, clock=clock, heartbeat=heartbeat
        )
        self._templates = TemplateIndex(store)
        self._positions = CyclePositionModel()
        # The AST's view of a call site, resolved once and frozen with the
        # grouping it decided. None means "static says nothing usable here".
        self._sites: dict[SourceKey, static.CallSite | None] = {}
        # Frozen, all three: source -> its row, group -> its row, and the row's
        # membership. Append-only.
        self._row_of: dict[SourceKey, SourceKey] = {}
        self._group_key: dict[_GroupId, SourceKey] = {}
        self._group_of: dict[SourceKey, _GroupId] = {}
        self._members: dict[SourceKey, list[SourceKey]] = {}
        # Rows in the order they first qualified. Display order is derived from
        # this and from structure; see `_display_order`.
        self._order: list[SourceKey] = []
        # Sources grouped by the runtime fallback, in assignment order. A new
        # runtime source only ever merges with one of these — never with a
        # statically-grouped loop.
        self._runtime: list[SourceKey] = []
        # Labels are frozen against a per-record `msg`. A call site using an
        # f-string has no template, so its stored `msg` is the *rendered*
        # string and changes with every record; recomputing the label each poll
        # would make such a row flicker. Keyed by membership size so a merge
        # still relabels.
        self._labels: dict[SourceKey, tuple[int, str]] = {}
        # A row's lexically enclosing loop, as a group id, resolved once.
        self._enclosing: dict[SourceKey, _GroupId | None] = {}
        # Poll index of the last one that moved this row's count, which is what
        # orders collapsed rows by recency.
        self._active_at: dict[SourceKey, int] = {}
        self._counted: dict[SourceKey, int] = {}
        self._polls = 0

    # -- the poll ----------------------------------------------------------

    def poll(self) -> list[LoopRow]:
        """Fold in whatever arrived, group it, and return the display rows."""
        bars = self._sources.poll()
        self._polls += 1
        # Before assignment, not after: grouping is frozen once made, so a
        # source assigned while its template is still unknown would take the
        # runtime path permanently and could never merge with its siblings.
        self._templates.learn(bar.source for bar in bars)
        by_source = {bar.source: bar for bar in bars}
        for bar in bars:
            if bar.source not in self._row_of:
                self._assign(bar, by_source)
        built = self._built()
        # Before ordering, not after: recency is one of the two things the
        # order reads, so folding it in afterwards would lay every row out one
        # poll behind the evidence.
        for key, row in built.items():
            if row.count > self._counted.get(key, -1):
                self._counted[key] = row.count
                self._active_at[key] = self._polls
        return self._ordered(built)

    @property
    def heartbeat(self) -> HeartbeatState:
        """Session-level liveness as of the last poll. See `SessionHeartbeat`."""
        return self._sources.heartbeat

    def bars(self) -> list[BarState]:
        """One entry per source location: the identity layer, ungrouped.

        Deliberately not what is drawn. A caller asking what lumberjack has
        been counting should get the answer the store would corroborate, which
        is per call site; a caller asking what is on screen wants `rows()`.
        """
        return self._sources.bars()

    # -- grouping ----------------------------------------------------------

    def _assign(self, bar: BarState, by_source: Mapping[SourceKey, BarState]) -> None:
        """Put a newly-qualified source in a row, once and for good."""
        source = bar.source
        if self._static_pending(source):
            # The file is readable and its template has not turned up yet, so
            # a decision now would be a guess with a frozen consequence. Cost
            # of waiting: this source draws no row for one more poll.
            return
        group = self._group_for(bar, by_source)
        key = self._group_key.get(group)
        if key is None:
            key = source
            self._group_key[group] = key
            self._members[key] = []
            self._order.append(key)
            if group.kind == "site" and self._sites.get(source) is None:
                self._runtime.append(source)
        self._group_of[source] = group
        self._row_of[source] = key
        self._members[key].append(source)

    def _group_for(
        self, bar: BarState, by_source: Mapping[SourceKey, BarState]
    ) -> _GroupId:
        site = self._read_site(bar.source)
        if site is not None:
            loop_lineno = site.loop_lineno
            if loop_lineno is None:
                # A repeating line that is not in a loop at all — recursion, or
                # a caller looping around it. Exact, and exactly one row.
                return _GroupId("site", bar.source)
            return _GroupId(
                "loop", SourceKey(bar.source.pathname, loop_lineno, site.func_name)
            )
        return self._runtime_group(bar, by_source)

    def _runtime_group(
        self, bar: BarState, by_source: Mapping[SourceKey, BarState]
    ) -> _GroupId:
        """Today's signal, for code the AST cannot see: period and worker.

        Equal periods say two lines fire once each per iteration; a shared
        worker says they *could* be the same loop rather than two unrelated
        ones whose rates happen to divide neatly. The worker half is the same
        check containment scoping needs, and load-bearing for the same reason:
        without it the three independent threads in `examples/demo.py pipeline`
        read as one structure.

        Scanned against sources already grouped this way rather than against
        every bar, so a runtime guess can never be folded into a statically
        grouped loop.
        """
        if bar.period:
            for other_source in self._runtime:
                other = by_source.get(other_source)
                if other is None or not other.period:
                    continue
                if not _same_loop_period(bar.period, other.period):
                    continue
                if not (bar.workers & other.workers):
                    continue
                return self._group_of[other_source]
        return _GroupId("site", bar.source)

    def _static_pending(self, source: SourceKey) -> bool:
        """Whether static analysis might still have something to say here.

        Only ever asked about a source with no row yet, and answering False is
        what gives it one — so nothing asks twice.
        """
        if not self._templates.pending(source):
            return False
        return static.analyze_file(source.pathname) is not None

    def _read_site(self, source: SourceKey) -> static.CallSite | None:
        """The AST's view of this call site, or None if it is untrustworthy.

        Cached on `self._sites` as it is resolved, because the veto in
        `_corroborated()` needs the same answer again for every row on every
        poll: the site is frozen with the grouping it decided.

        None covers every uncertain case at once — no source on disk, an
        unrecognised line, a template that did not survive to runtime, and
        genuine drift — because the caller does the same thing with all of
        them: fall back to what the timing says.
        """
        self._sites[source] = None
        stored_msg = self._templates.template(source)
        if stored_msg is None:
            return None
        if not static.template_matches(source.pathname, source.lineno, stored_msg):
            return None
        structure = static.analyze_file(source.pathname)
        if structure is None:  # pragma: no cover - template_matches just read it
            return None
        site = structure.call_sites.get(source.lineno)
        # `funcName` is the second half of the identity and the AST knows it
        # too, so disagreement means the line moved in a way the template check
        # happened not to catch.
        if site is None or site.func_name != source.func_name:
            return None
        self._sites[source] = site
        return site

    # -- rows --------------------------------------------------------------

    def rows(self) -> list[LoopRow]:
        """The display rows, in display order. Empty before the first poll."""
        return self._ordered(self._built())

    def _built(self) -> dict[SourceKey, LoopRow]:
        """One row per group, in arrival order and without depths yet."""
        bars = {bar.source: bar for bar in self._sources.bars()}
        # Membership only ever grows and a bar is never withdrawn, so every
        # member of every row is in `bars` — no row can come out empty.
        return {key: self._build(key, bars) for key in self._order}

    def _ordered(self, built: Mapping[SourceKey, LoopRow]) -> list[LoopRow]:
        order = self._display_order(built)
        depths = self._depths(built, order)
        return [dataclasses.replace(built[key], depth=depths[key]) for key in order]

    def _build(self, key: SourceKey, bars: Mapping[SourceKey, BarState]) -> LoopRow:
        members = self._members[key]
        states = [bars[member] for member in members]
        # The busiest member is the loop's clock. Where merged call sites fire
        # unequal numbers of times — a conditional error line in the body — a
        # line that fires every iteration is a better measure of the loop's
        # length than one that fires sometimes. Max of monotone counts is
        # monotone, which is what eviction safety needs.
        clock = max(states, key=lambda state: (state.count, state.source))
        parent = self._parent_row(key, clock)
        # `clock.total` is a ratio against *one specific source* — whichever
        # slower same-worker line the runtime model froze containment against.
        # It only means "how many of me fit in one of my parent" if that source
        # is in the row actually drawn as the parent, and three things here can
        # make it a different row: the ratio source may have merged into this
        # very row, `_parent_row` may substitute a lexical parent the ratio was
        # never measured against, and `_corroborated` only asks whether the
        # drawn parent lexically encloses the child — not whether it is the row
        # the number came from.
        #
        # Getting this wrong is not a transient miss. `cycle_current` rebases
        # on the ratio source's firings, so the count never overruns the wrong
        # total and the pulse-withdrawal path that catches every other bad
        # estimate never fires: a stable, confident, wrong percentage. That is
        # the one thing Principle 10 does not license.
        measured_against = self._row_of.get(clock.parent) if clock.parent else None
        total = (
            clock.total
            if parent is not None
            and measured_against == parent
            and self._corroborated(clock, parent)
            else None
        )
        return LoopRow(
            key=key,
            members=tuple(members),
            label=self._build_label(key, members),
            clock=clock.source,
            count=clock.count,
            period=clock.period,
            parent=parent,
            total=total,
            cycle_current=clock.cycle_current,
            # A row is alive while any of its call sites is: a body whose last
            # line is conditional must not retire the loop between the two.
            idle=all(state.idle for state in states),
            position=self._position_of(key, clock, states),
        )

    def _position_of(
        self, key: SourceKey, clock: BarState, states: Sequence[BarState]
    ) -> CyclePosition | None:
        """Where this iteration has got to, when the loop is slow enough to ask.

        The clock member's period is what the criterion is measured against,
        for the same reason it drives the rate column: it is the member that
        fires every iteration, so its interval is the loop's.
        """
        group = self._group_of.get(key)
        loop = group.at if group is not None and group.kind == "loop" else None
        return self._positions.of(
            key,
            loop=loop,
            period=clock.period,
            sites=self._sites,
            states=states,
            label_of=self._stage_label,
        )

    def _stage_label(self, source: SourceKey) -> str:
        """One call site's template, as the name of a stage within the body.

        Recomputed rather than frozen, unlike a row's own label: the stage
        *is* what changes as the iteration advances, so caching it would be
        caching the answer. There is nothing to flicker either — a source
        without a stable template cannot be in a statically grouped row, so
        every stage here has one.
        """
        template = self._templates.template(source)
        described = describe_template(template) if template else ""
        # `ntpath.basename`, on every platform — see `SourceKey.format`.
        where = ntpath.basename(source.pathname)
        return described or f"{where}:{source.lineno}"

    def _parent_row(self, key: SourceKey, clock: BarState) -> SourceKey | None:
        """Which row this one runs inside: the AST first, then the timing.

        Static is **place-at-birth**. A lexically nested loop is nested from
        the first record, so the row can be positioned correctly straight away
        rather than sitting somewhere wrong until the ratio confirms — and it
        is exact, where the runtime answer is a guess that happens to be right
        most of the time.
        """
        enclosing = self._read_enclosing(key)
        static_parent = (
            self._group_key.get(enclosing) if enclosing is not None else None
        )
        if static_parent is not None and static_parent != key:
            return static_parent
        if clock.parent is None:
            return None
        parent = self._row_of.get(clock.parent)
        return None if parent == key else parent

    def _read_enclosing(self, key: SourceKey) -> _GroupId | None:
        """Which group would hold the loop lexically enclosing this row's.

        Cached on `self._enclosing`, and safe to cache, because the group it
        is derived from is frozen: the answer is a property of one file at
        one moment, asked once. Resolving it per poll instead would mean an
        `os.stat` per row per redraw to re-ask a question whose inputs cannot
        move.

        `self._group_key` — which row that group became, if any — is
        deliberately *not* folded in here and stays a fresh lookup at every
        call site: unlike this answer, it grows as new rows qualify, so
        caching it would freeze a row's parent at whichever poll first asked.
        """
        if key in self._enclosing:
            return self._enclosing[key]
        self._enclosing[key] = None
        group = self._group_of.get(key)
        if group is None or group.kind != "loop":
            return None
        structure = static.analyze_file(group.at.pathname)
        if structure is None:  # pragma: no cover - the group came from this file
            return None
        loop = structure.loops.get(group.at.lineno)
        if loop is None or loop.parent is None:
            return None
        enclosing = _GroupId(
            "loop", SourceKey(group.at.pathname, loop.parent, loop.func_name)
        )
        self._enclosing[key] = enclosing
        return enclosing

    def _corroborated(self, clock: BarState, parent: SourceKey) -> bool:
        """Whether the AST agrees the parent can lexically enclose the child.

        True whenever static analysis has nothing to say about either side,
        which keeps the shipped behaviour for code it cannot read. It only
        returns False on an active contradiction: both files were readable,
        both templates matched, and the claimed parent's loop is nowhere in the
        child's chain of enclosing loops.

        That is the `phases` case. The once-per-stage `log.info` is a slow
        repeating source, period ordering reads it as an enclosing loop, and
        the ratio between the two periods becomes a total of 49 for a loop that
        runs 40 times. The AST says the child is a top-level loop in a
        different function, so the claim is refused — and only the claim. The
        indent stays, because the stage function genuinely is called from
        inside that loop; what static cannot see is a *call graph*, and it says
        nothing about cross-function containment rather than denying it.

        Scope is the file *and* the function, and both halves matter. Different
        files is the same refusal as different functions, only more so; and
        comparing loop line numbers across two files would let `run()` in one
        corroborate `run()` in another by coincidence of line numbering.
        """
        child_site = self._sites.get(clock.source)
        # The parent row speaks through its *canonical* member rather than its
        # busiest one, which can change between polls: a veto that flickered
        # would put a total on screen and take it off again. Every member of a
        # statically grouped row shares one loop body, so they agree anyway.
        parent_site = self._sites.get(parent)
        if child_site is None or parent_site is None:
            return True
        same_scope = (
            clock.source.pathname == parent.pathname
            and parent_site.func_name == child_site.func_name
        )
        return same_scope and parent_site.loop_lineno in child_site.loop_chain[:-1]

    def _build_label(self, key: SourceKey, members: Sequence[SourceKey]) -> str:
        """The template for a single call site; the function for a merged loop.

        Cached by membership size — a merge is the only thing that can change
        the answer, so a cache hit costs one dict lookup and a comparison
        rather than re-describing a template every poll.

        A merged row has several templates and no reason to prefer one, so it
        is named for the loop instead — `demo.py:188 run_siblings()`, the loop
        statement rather than any of its lines. A single-site row has exactly
        one template, and it is a better name than a file and a line number:
        `reconciling batch …` says what the loop *does*.

        A row merged by the runtime fallback has no loop statement to point at,
        so it drops the line number rather than borrowing one member's:
        `foo.py bar()`. The absent number is the honest part — the row covers
        several lines and nothing here knows which one the `for` is on.
        """
        # Only the `describe_template()` branch below is bounded by
        # `MAX_LABEL`; the other three return an unbounded string, and a
        # long one starves the bar it shares a line with.
        # lumberjack: see issue #99
        cached = self._labels.get(key)
        if cached is not None and cached[0] == len(members):
            return cached[1]
        if len(members) > 1:
            group = self._group_of.get(key)
            if group is not None and group.kind == "loop":
                label = group.at.format()
            else:
                # `ntpath.basename` on every platform — see `SourceKey.format`
                # for why not `os.path`: a record's pathname may be foreign.
                base = ntpath.basename(key.pathname)
                label = f"{base} {key.func_name}()"
        else:
            template = self._templates.template(key)
            described = describe_template(template) if template else ""
            label = described or key.format()
        self._labels[key] = (len(members), label)
        return label

    # -- order -------------------------------------------------------------

    def _display_order(self, rows: Mapping[SourceKey, LoopRow]) -> list[SourceKey]:
        """Structure decides placement; liveness decides which subtree first.

        Two rules, and both are discrete. `depth_first_order` puts every row
        after the row it runs inside, breaking ties by arrival. Then whole
        subtrees — never individual rows, which would tear a parent from its
        children — are split into those with something still running and those
        entirely quiet, the second group ordered by how recently it last moved.

        The screen budget is real whether or not it is acknowledged: rich crops
        at terminal height regardless, so the only question is which rows are
        nearest the top when it does. Filling it deliberately with active work,
        then recently active, then long since idle, is the whole of it — there
        is no "…N more" row and never will be, because the row count came down
        by the display being right rather than by rows being suppressed.
        """
        present = [key for key in self._order if key in rows]
        structural = depth_first_order(present, {k: r.parent for k, r in rows.items()})
        # Reading `roots[parent]` here relies on `depth_first_order` having
        # already placed every parent ahead of its children, which `_depths`
        # states for its own walk and this one needs just as much: a child
        # seen first would be rooted at itself and tear off its own subtree.
        #
        # Not `roots.get(parent, key)`, which ruff offers: `parent` is
        # `SourceKey | None`, the `in` test narrows it and `.get()` does not,
        # so the tidier form fails `mypy --strict src` — a required check.
        roots: dict[SourceKey, SourceKey] = {}
        for key in structural:
            parent = rows[key].parent
            # SIM401 would have this `roots.get(parent, key)`, which does not
            # typecheck: `parent` is `SourceKey | None`, `dict.get` takes the
            # key type, and `in` takes `object`. The rewrite trades a required
            # CI check for a line of prose.
            roots[key] = roots[parent] if parent in roots else key  # noqa: SIM401
        subtrees: dict[SourceKey, list[SourceKey]] = {}
        for key in structural:
            subtrees.setdefault(roots[key], []).append(key)
        live = {roots[key] for key in structural if not rows[key].idle}
        running = [root for root in subtrees if root in live]
        quiet = sorted(
            (root for root in subtrees if root not in live),
            key=lambda root: -self._active_at.get(root, 0),
        )
        return [key for root in (*running, *quiet) for key in subtrees[root]]

    def _depths(
        self, rows: Mapping[SourceKey, LoopRow], order: Sequence[SourceKey]
    ) -> dict[SourceKey, int]:
        """How far each row is indented, walked from the parent links.

        Iterative over an order that already puts parents first, so a chain
        costs one lookup per row rather than one walk per row. Anything whose
        parent has not been seen — the remnant of a containment cycle, which
        `depth_first_order` appends rather than drops — sits at the top level.
        """
        depths: dict[SourceKey, int] = {}
        for key in order:
            parent = rows[key].parent
            depths[key] = depths[parent] + 1 if parent in depths else 0
        return depths
