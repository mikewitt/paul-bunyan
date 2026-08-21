"""`record.msg` per source location: the label, and the drift guard.

stdlib keeps the message **template** and the **data** in separate attributes
whenever the call site uses lazy `%`-formatting, which `ruff`'s G001–G004
enforce without knowing lumberjack exists:

    log.debug("batch %d: validating", i)   ->  msg='batch %d: validating'
    log.debug(f"batch {i}: validating")    ->  msg='batch 5: validating'

The store already writes both columns. So for a lazily-formatted call site
`record.msg` is a **constant per source location** and a human-readable
description of what that line does — already captured, already structured, and
requiring no parsing of rendered text. Reading it is the same category of act
as reading `lineno` or `threadName`, which is why it is not the message-content
inference Principle 3 forbids.

It buys two things:

* **A label.** `reconciling batch …` rather than `demo.py:132 reconcile()`.
* **The drift guard.** `static.template_matches()` compares this string against
  what the AST reads at `file:lineno`, and refuses every structural claim about
  a file that has moved under the running process. Static analysis is unusable
  without it, so the harvest below is what unlocks static analysis at all.

An f-string at the call site destroys the template irrecoverably: `msg` becomes
the rendered string, which is not constant per location. Nothing detects that
here and nothing needs to — the template simply fails to match the AST, the
static path is refused, and the row falls back to `file:line func()`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from lumberjack.schema import SourceKey

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lumberjack.store import RecordStore

#: How far back to look for a source's `msg`, one entry per attempt.
#:
#: Escalating rather than fixed, because the two cases pull opposite ways. A
#: source in a live loop is in the last handful of records and 64 rows finds it
#: for about 0.8ms; a once-per-stage announcement line can be a thousand
#: records back while a fast loop runs underneath it, and that one is worth a
#: 12ms read exactly once. Measured at ~12µs a row against a 1M-row store (see
#: `heartbeat.MESSAGE_LOOKBACK` for the same measurement), so these are roughly
#: 0.8ms, 3ms and 12ms.
#:
#: Running out of attempts is a give-up, recorded as such: the source keeps its
#: `file:line func()` label, takes the runtime grouping path, and is never
#: looked for again. Without that a source that qualified in a burst and then
#: went quiet would cost a store read on every redraw for the rest of the run.
LOOKBACKS = (64, 256, 1024)

#: One `%`-conversion, in the form `logging` itself will apply it. Covers the
#: mapping key (`%(name)s`), the flags, the width and precision — including the
#: `*` forms — the length modifier stdlib accepts and ignores, and the
#: conversion character. `%%` matches too and is put back as a literal `%`.
_CONVERSION = re.compile(
    r"%(?:\([^)]*\))?[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa%]"
)

#: What a conversion is replaced with. A single character, because the point of
#: the label is the *words*: `row …: schema validated` reads as a description
#: of the line, where `row %d: schema validated` reads as source code.
_PLACEHOLDER = "…"

#: Longest label a row may *carry*. A log template can be a paragraph, and a
#: frame holding one is a frame that has to be cropped by whoever draws it.
#:
#: This is a bound on content, not on layout, and the two are separate on
#: purpose. How much of a *line* the label may occupy depends on how wide the
#: terminal is, so it is decided at render time by `_RowTextColumn` — which
#: also means this number no longer has to be small enough to leave room for a
#: bar at 80 columns. It only has to stop a paragraph reaching the frame.
MAX_LABEL = 56


def clip_label(label: str) -> str:
    """`label`, no longer than `MAX_LABEL`, ellipsis included in the budget.

    Every branch that produces a row label goes through here. A function name
    is as capable of being 200 characters long as a message template is, and
    for a while only the template branch was bounded — see issue #99.
    """
    if len(label) <= MAX_LABEL:
        return label
    return label[: MAX_LABEL - 1].rstrip() + _PLACEHOLDER


def describe_template(template: str) -> str:
    """A message template as a row label: first line, no format specifiers.

    Substitution rather than interpolation of the newest values. Interpolating
    would make the label change on every record, which is a count wearing a
    description's clothes — the row would flicker and say nothing more.
    """
    first_line = template.partition("\n")[0].strip()
    label = _CONVERSION.sub(
        lambda match: "%" if match.group(0) == "%%" else _PLACEHOLDER, first_line
    )
    return clip_label(" ".join(label.split()))


class TemplateIndex:
    """`record.msg` per source location, harvested from a bounded tail read.

    **Why a tail read rather than the delta.** `count_by_source_since()` is an
    aggregate — counts, timestamps and workers — and carries no message at all,
    by design: it is what makes a redraw cost what arrived rather than what the
    store holds. `msg` is not an aggregate, and adding it to that query would
    put a string column in the group key of the one query on the hot path.

    **Why not the renderer's per-record callback**, which sees every `msg` for
    free. Because it does not see every *record*: anything another writer put
    in the store — a second thread's renderer, a future OTel bridge — has a bar
    and would have no label. Store, then render (Principle 2) is the rule, and
    the exception is not worth the divergence.

    So the cost is a read, and the read is made rare rather than cheap. It
    happens only on a poll where some source still has no template and has not
    exhausted `LOOKBACKS`, which in practice means the first poll or two after
    a new source appears — a structural event — and never again.
    """

    def __init__(self, store: RecordStore, *, lookbacks: tuple[int, ...] = LOOKBACKS):
        self._store = store
        self._lookbacks = lookbacks
        # None is a recorded give-up rather than a missing entry, which is what
        # stops a source that never turns up in the window being looked for
        # forever.
        self._known: dict[SourceKey, str | None] = {}
        self._attempts: dict[SourceKey, int] = {}

    def template(self, source: SourceKey) -> str | None:
        """The stored `msg` for this source, or None if it is not known yet."""
        return self._known.get(source)

    def pending(self, source: SourceKey) -> bool:
        """Whether a later poll might still turn this source's template up.

        A caller deciding between the static path and the runtime one wants to
        wait rather than commit while this is True: grouping is frozen once
        assigned, so guessing early and being wrong costs a row that can never
        merge.
        """
        return source not in self._known and self._attempts.get(source, 0) < len(
            self._lookbacks
        )

    def learn(self, wanted: Iterable[SourceKey]) -> None:
        """Read the store's tail once, if any of `wanted` still needs it.

        Every source the read passes is recorded, not only the ones asked
        for — the rows are already materialized, and a source that has not
        earned a bar yet often ends up being the *parent* of one that has.
        `phases`' once-per-stage line is exactly that, and it is the source
        whose template decides whether the running stage gets a fabricated
        total or an honest pulse.
        """
        pending = [source for source in wanted if self.pending(source)]
        if not pending:
            return
        # The largest lookback any pending source has earned. Escalating on the
        # max rather than the min terminates: a stuck source works its way
        # through the list and is then written off, instead of holding every
        # later arrival at the cheapest read forever.
        attempt = max(self._attempts.get(source, 0) for source in pending)
        for record in self._store.recent(n=self._lookbacks[attempt]):
            # Newest wins: `recent()` is oldest-first, so a later row overwrites
            # an earlier one for the same location.
            self._known[SourceKey(record.pathname, record.lineno, record.func_name)] = (
                record.msg
            )
        for source in pending:
            if source in self._known:
                continue
            self._attempts[source] = self._attempts.get(source, 0) + 1
            if self._attempts[source] >= len(self._lookbacks):
                self._known[source] = None
