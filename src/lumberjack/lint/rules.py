"""The seven rules: what each one flags, and the two principles behind them.

`lint.py` used to be one module holding four separate jobs — the rules,
report formatting, the agent-facing rules block, and the CLI — that had
stopped sharing much beyond a name; see the package's `__init__.py` for the
four-way split. This module holds the rule generators themselves, plus the
scope-grouping (`_Scope`, `_scopes`) and the small prose helpers (`_subject`,
`_where`, `_span`, `_logging_descendant`, `_before_message`) that only the
rules need — everything downstream of "what a function's loops and call
sites look like" and upstream of "how a finding gets printed".

## Two rules that shape every finding below

**Idiomatic logging first, `track()` second.** Where the display cannot infer
something, the first question is whether an ordinary log line would have
supplied it. Only when the answer is no does instrumentation become the
recommendation, and then as a `also:` note beneath the log-line fix rather
than instead of it. A linter that opens with "wrap this in `track()`" has
given the expensive advice first.

**A false positive costs more than a miss.** A linter people stop trusting is
worth nothing, and the raw structural facts here are far too common to report
raw: this repository alone holds forty-odd loops with no log line in the body,
and nearly all of them are plumbing — `for worker in workers: worker.start()`,
`for line in text.splitlines(): …`. Static analysis can see that a loop logs
nothing. It cannot see whether the loop is worth watching; `for w in workers:
w.start()` and `for row in rows: process(row)` are the same shape.

So the silent-loop findings are gated on a signal that *is* visible:
**evidence that somebody wanted this code narrated.** A silent loop is
reported when its own function logs somewhere else, or when a loop nested
inside it logs. Both mean the author is already narrating this code and this
loop is the dark part. A silent loop in a function that logs nothing at all is
counted in the summary and not reported, because nothing in the source
distinguishes it from plumbing — see the note printed at the end of a run.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Final

from lumberjack import static
from lumberjack.lint import Finding

#: A body this much larger than its narration is a body one line cannot
#: describe. Counted as statements-that-are-not-the-log-call, so five means
#: `run_sequence`'s shape in `examples/demo.py` with four of its five log
#: lines deleted. Deliberately high: every one-line loop body in this
#: repository sits at one or two, so the gap between a normal loop and a
#: genuinely under-narrated one is wide and the threshold is nowhere near it.
SLOW_BODY_STATEMENTS: Final = 5

#: Loops run in sequence in one function before "which stage is running?"
#: becomes a question the display cannot answer. Two loops in a row is a
#: shape, three is a pipeline.
STAGE_LOOPS: Final = 3

#: Log calls in a file, none of them inside a loop, before saying so is worth
#: a line. One or two is a small module; several is a program that narrates
#: itself and still draws no bar.
ONESHOT_CALLS: Final = 3


# --------------------------------------------------------------------------
# Scope grouping
#
# Every rule below asks some version of "what else does this function do?",
# and `func_lineno` is what makes that exact. Grouping by `func_name` would
# merge two same-named methods on different classes into one scope, which
# this repository already has (`progress/sources.py` and `progress/tasks.py`
# each hold a `_depth`).
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _Scope:
    loops: tuple[static.Loop, ...]
    sites: tuple[static.CallSite, ...]

    @property
    def outer_loops(self) -> tuple[static.Loop, ...]:
        """Loops at depth 1 — the ones that run in sequence, not nested."""
        return tuple(loop for loop in self.loops if loop.depth == 1)

    @property
    def sites_outside_loops(self) -> tuple[static.CallSite, ...]:
        return tuple(site for site in self.sites if not site.loop_chain)


def _scopes(structure: static.FileStructure) -> dict[int | None, _Scope]:
    loops: dict[int | None, list[static.Loop]] = {}
    sites: dict[int | None, list[static.CallSite]] = {}
    for loop in structure.loops.values():
        loops.setdefault(loop.func_lineno, []).append(loop)
    for site in structure.call_sites.values():
        sites.setdefault(site.func_lineno, []).append(site)
    return {
        key: _Scope(tuple(loops.get(key, ())), tuple(sites.get(key, ())))
        for key in loops.keys() | sites.keys()
    }


def _logging_descendant(
    loop: static.Loop, structure: static.FileStructure
) -> static.Loop | None:
    """A loop nested anywhere inside `loop` that does have a call site."""
    for candidate in structure.loops.values():
        if not candidate.call_sites:
            continue
        parent = candidate.parent
        while parent is not None:
            if parent == loop.lineno:
                return candidate
            parent = structure.loops[parent].parent
    return None


def _span(loop: static.Loop) -> str:
    if loop.end_lineno > loop.lineno:
        return f"lines {loop.lineno}-{loop.end_lineno}"
    return f"line {loop.lineno}"


def _subject(func_name: str) -> str:
    """How to name a scope in prose.

    `funcName` reads `<module>` at module level, which is a true fact about
    the record and a strange thing to say to a person — "`<module>()` logs at
    line 1" reads as a bug in the linter rather than advice.
    """
    if func_name == static.MODULE_SCOPE:
        return "this module"
    return f"{func_name}()"


def _where(loop: static.Loop) -> str:
    scope = "" if loop.func_name == static.MODULE_SCOPE else f" in {loop.func_name}()"
    return f"the `{loop.kind}` loop at {_span(loop)}{scope}"


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def _silent_loops(
    structure: static.FileStructure,
    scopes: dict[int | None, _Scope],
    *,
    all_loops: bool = False,
) -> Iterator[Finding]:
    """A loop with no log line in its body — #40's first two rows.

    Both rows are the same structural fact and differ only in where the
    surrounding narration went, so they are found together and split on the
    evidence. The gate is the module docstring's: report only where the
    author has already shown they wanted this code narrated.

    `all_loops` lifts the gate. The finding is real either way — the loop
    genuinely is invisible — and the flag exists so the fact is reachable
    rather than absent. It is off by default because on this repository it
    turns 4 findings into 159, nearly all of them `for w in workers:
    w.start()`.
    """
    for loop in structure.loops.values():
        if loop.call_sites:
            continue
        scope = scopes[loop.func_lineno]
        around = scope.sites_outside_loops
        if around:
            # The author announced the loop and then went quiet. This is the
            # most specific diagnosis available, so it wins over the general
            # one below even when a nested loop also logs.
            yield Finding(
                rule="loop-logs-around",
                pathname=structure.pathname,
                lineno=loop.lineno,
                what=(
                    f"{_subject(loop.func_name)} logs at line {around[0].lineno} "
                    f"but not inside {_where(loop)}. A line before or after a "
                    f"loop says it ran; only a line inside it can say how far "
                    f"along it is."
                ),
                fix=(
                    "add one log call in the loop body, naming the item: "
                    '`log.debug("processing %s", item)`. One record per '
                    "iteration is what the display turns into a bar."
                ),
                also=(
                    "if you also want a determinate total rather than a pulse, "
                    "`for item in lumberjack.track(items):` states it — but the "
                    "log line comes first and costs no dependency."
                ),
            )
            continue

        nested = _logging_descendant(loop, structure)
        if nested is not None:
            yield Finding(
                rule="loop-not-logged",
                pathname=structure.pathname,
                lineno=loop.lineno,
                what=(
                    f"{_where(loop).capitalize()} has no log line in its body, but "
                    f"the loop nested inside it at line {nested.lineno} does. The "
                    f"display sees the inner work and has nothing to say which "
                    f"outer iteration it belongs to — and no outer period to take "
                    f"a ratio against, which is where the inner loop's total "
                    f"would come from."
                ),
                fix=(
                    "add one log call at the top of the outer body: "
                    '`log.debug("batch %d", batch)`. That single line is what '
                    "gives the inner bar a total as well as the outer one a row."
                ),
            )
            continue

        if any(site.loop_chain for site in scope.sites):
            other = next(site for site in scope.sites if site.loop_chain)
            yield Finding(
                rule="loop-not-logged",
                pathname=structure.pathname,
                lineno=loop.lineno,
                what=(
                    f"{_where(loop).capitalize()} has no log line in its body. "
                    f"{_subject(loop.func_name)} does log inside another loop (line "
                    f"{other.lineno}), so this loop is the part of it the display "
                    f"cannot see at all."
                ),
                fix=(
                    "add a log call in this body too, of the same shape as the "
                    f"one at line {other.lineno}. A loop that logs nothing is "
                    "invisible, and no amount of inference recovers it."
                ),
            )
            continue

        if all_loops:
            # No narration anywhere in the scope, so nothing in the source
            # separates this from plumbing. The observation still stands and
            # the advice is conditional on something only the author knows.
            yield Finding(
                rule="loop-not-logged",
                pathname=structure.pathname,
                lineno=loop.lineno,
                what=(
                    f"{_where(loop).capitalize()} has no log line in its body, so "
                    f"the display cannot see it at all. Nothing else in "
                    f"{_subject(loop.func_name)} logs either, so the source gives no "
                    f"sign of whether this is work worth watching or plumbing — "
                    f"which "
                    f"is why it takes --all-loops to say so."
                ),
                fix=(
                    "if an iteration of this loop is slow enough that you would "
                    'wait on it, add one line inside the body: `log.debug("…%s", '
                    "item)`. If it is not, there is nothing to do here."
                ),
                # Reported on request, so it must not silently gate a build
                # that the default run passes.
                gates=False,
            )


def _slow_bodies(structure: static.FileStructure) -> Iterator[Finding]:
    """A body doing a lot of work with a single line narrating it (#53)."""
    for loop in structure.loops.values():
        if len(loop.call_sites) != 1:
            continue
        work = loop.body_statements - 1
        if work < SLOW_BODY_STATEMENTS:
            continue
        (site,) = loop.call_sites
        yield Finding(
            rule="one-line-slow-body",
            pathname=structure.pathname,
            lineno=loop.lineno,
            what=(
                f"{_where(loop).capitalize()} runs {work} statements per "
                f"iteration and one log line (line {site.lineno}) narrates all "
                f"of them. If an iteration is slow, the display can tick once "
                f"per iteration and cannot say where inside one it is."
            ),
            fix=(
                "add a line per phase of the body — "
                '`log.debug("batch %d: validating checksums", batch)` — in the '
                "order the phases run. Their textual order is then the position "
                "within an iteration, which is a second, faster-moving row "
                "under the loop's own."
            ),
        )


def _stage_announcements(
    structure: static.FileStructure, scopes: dict[int | None, _Scope]
) -> Iterator[Finding]:
    """A multi-stage routine with nothing naming the stage that is running.

    Restricted to *sibling loops in one function*, which is lexical and
    exact. The other shape of the same defect — a function calling several
    stage functions in sequence, which is what `run_phases` in
    `examples/demo.py` actually does — needs a same-file call graph and is
    lumberjack: see issue #64.
    """
    for scope in scopes.values():
        stages = scope.outer_loops
        if len(stages) < STAGE_LOOPS or not all(loop.call_sites for loop in stages):
            continue
        if scope.sites_outside_loops:
            continue
        first = stages[0]
        yield Finding(
            rule="no-stage-announcements",
            pathname=structure.pathname,
            lineno=first.lineno,
            what=(
                f"{_subject(first.func_name)} runs {len(stages)} loops in sequence "
                f"(lines {', '.join(str(loop.lineno) for loop in stages)}) and "
                f"logs nothing between them. Each stage is visible on its own, "
                f"and nothing names the one running now or marks where one ends "
                f"and the next begins."
            ),
            fix=(
                "announce each stage with one line above its loop: "
                '`log.info("stage 1: discovering input files")`. That is the '
                "signal a finished stage collapses on, and it is worth having "
                "in the log file regardless."
            ),
            also=(
                "`with lumberjack.task('discovering input files'):` around each "
                "stage gives the same names plus a real hierarchy. Reach for it "
                "only if the announcement line is not enough — telling 'A "
                "encloses B' from 'A precedes B' is the one thing logs cannot "
                "settle."
            ),
        )


def _before_message(method: str) -> str:
    """What a suggested call must pass before the message, for `method`.

    Every `fix:` below is meant to be pasted, so the suggested call has to be
    one that runs. `logger.log()` takes the level first and the other six do
    not, which `static.message_arg_index()` already knows — asking it beats
    matching on the name here and in the next rule.
    """
    return "level, " if static.message_arg_index(method) else ""


def _wrappers(structure: static.FileStructure) -> Iterator[Finding]:
    """A logging wrapper with no `stacklevel=` — #37's one-keyword fix."""
    for site in structure.call_sites.values():
        if site.message_kind != "parameter" or site.has_stacklevel:
            continue
        yield Finding(
            rule="wrapper-no-stacklevel",
            pathname=structure.pathname,
            lineno=site.lineno,
            what=(
                f"{_subject(site.func_name)} forwards its own parameter to "
                f"`log.{site.method}()` with no `stacklevel=`, which is the "
                f"shape of a logging wrapper. Identity is the source location, "
                f"so every call to {site.func_name}() anywhere in the program "
                f"reports *this* line and they all collapse onto one bar."
            ),
            fix=(
                f"pass the caller's frame through: `log.{site.method}("
                f"{_before_message(site.method)}message, *args, stacklevel=2)`. "
                f"One keyword, and every call site gets its own identity back."
            ),
        )


def _fstrings(structure: static.FileStructure) -> Iterator[Finding]:
    """An f-string log call, which destroys the template unrecoverably."""
    for site in structure.call_sites.values():
        if site.message_kind != "fstring":
            continue
        yield Finding(
            rule="fstring-log-call",
            pathname=structure.pathname,
            lineno=site.lineno,
            what=(
                "the message is built with an f-string, so `record.msg` holds "
                "rendered text and the template is gone. A source location's "
                "label comes from its template, so this row can have a position "
                "and a count but no name."
            ),
            fix=(
                f"use lazy %-formatting: `log.{site.method}("
                f'{_before_message(site.method)}"row %d parsed", i)` '
                f"rather than an f-string. The template and the data then land "
                f"in separate fields, which is also what makes the log file "
                f"greppable."
            ),
            also=(
                "ruff's G001-G004 enforce this across a codebase, including the "
                "`%`, `+` and `.format()` variants this linter does not look "
                "for. Running them is the fuller answer."
            ),
        )


def _nothing_repeating(
    structure: static.FileStructure, scopes: dict[int | None, _Scope]
) -> Iterator[Finding]:
    """A routine that narrates itself and never repeats (`oneshot`).

    Scoped to a function rather than a file, which measurement chose: across
    this repository's 43 files the per-scope rule fires exactly once, on
    `run_oneshot` in `examples/demo.py`, which is the shape #40 names. The
    file-level version missed it — `demo.py` has plenty of repeating sources
    elsewhere — and instead fired on two test modules, which is the wrong
    answer twice over.
    """
    for scope in scopes.values():
        sites = scope.sites_outside_loops
        if scope.loops or len(sites) < ONESHOT_CALLS or len(sites) != len(scope.sites):
            continue
        first = sites[0]
        yield Finding(
            rule="nothing-repeating",
            pathname=structure.pathname,
            lineno=first.lineno,
            what=(
                f"{_subject(first.func_name)} logs {len(sites)} times and contains "
                f"no loop at all, so nothing here repeats. No source has a period, "
                f"so nothing will draw a bar — the display can show a heartbeat and "
                f"the newest line, and that is the whole of it."
            ),
            fix=(
                "if there is repeated work here that is not written as a loop, or "
                "a loop worth watching further down, log inside it. If the work "
                "is genuinely one-shot then nothing is wrong: a display with no "
                "bars is the honest answer, and this line exists so that an empty "
                "screen does not read as a bug."
            ),
            # Not a defect. A one-shot routine is allowed to be one-shot, and
            # failing a build over it would be absurd.
            gates=False,
        )
