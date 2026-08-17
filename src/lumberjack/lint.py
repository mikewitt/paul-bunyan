"""The instrumentation linter: which line to add, and where.

    python -m lumberjack.lint [PATH ...]
    python -m lumberjack.lint --agent-rules >> CLAUDE.md

The value ladder has three rungs — drop `init()` in, log idiomatically,
instrument with `track()`/`task()` — and the middle one is the pitch. It asks
for no dependency and no lumberjack-shaped thinking, only the kind of logging
that makes a log file worth reading anyway. **This module is the only thing
that can say what that rung wants from a specific codebase.** The runtime
display can show that something is missing; it cannot say which line to add or
where. So this is the mechanism that moves people up the ladder, not a report
about it (issue #40).

Everything here reads `static.analyze_file()` and nothing else. No file is
imported, executed or evaluated, and no runtime data is involved.

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

import argparse
import dataclasses
import os
import sys
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from typing import Final

from lumberjack import static

#: Findings in #40's order of value, highest first. Output is sorted by this
#: rank before path and line, so the first thing on screen is the thing most
#: worth fixing rather than whatever sorts first alphabetically.
RULE_ORDER: Final[tuple[str, ...]] = (
    "loop-not-logged",
    "loop-logs-around",
    "one-line-slow-body",
    "no-stage-announcements",
    "wrapper-no-stacklevel",
    "fstring-log-call",
    "nothing-repeating",
)

_RULE_RANK: Final[dict[str, int]] = {rule: i for i, rule in enumerate(RULE_ORDER)}

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

#: Directories never walked. Everything here is either not the user's code or
#: not code at all, and a linter that reports on `.venv` is a linter nobody
#: runs twice. Any directory whose name starts with `.` is skipped too.
_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One thing to change, where to change it, and what to change it to.

    `what` states the observation, `fix` states the edit — and `fix` is always
    rung 2, an ordinary log line. `also` is where `track()` is allowed to
    appear, beneath the cheaper answer and never instead of it.
    """

    rule: str
    pathname: str
    lineno: int
    what: str
    fix: str
    #: The rung-3 note: what instrumentation would add on top. Optional, and
    #: never the whole answer.
    also: str | None = None
    #: Whether this finding should make the process exit non-zero. False for
    #: an observation that is not a defect — `nothing-repeating` describes a
    #: one-shot script correctly, and failing a build over it would be absurd.
    gates: bool = True

    @property
    def sort_key(self) -> tuple[int, str, int]:
        return (_RULE_RANK[self.rule], self.pathname, self.lineno)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Report:
    """Findings plus the counts that say what was looked at.

    The counts are not decoration. `silent_loops` against `len(findings)` is
    how a reader sees that the linter is holding things back deliberately
    rather than failing to notice them.
    """

    findings: tuple[Finding, ...]
    files: int
    loops: int
    call_sites: int
    #: Call sites inside a loop body — the ones that can become a bar.
    repeating_call_sites: int
    #: Loops with no log line in the body, reported or not.
    silent_loops: int
    #: Files skipped because they never import `logging`, so nothing in them
    #: is known to be a log call. Counted rather than silent: "the linter
    #: said nothing" and "the linter did not look" must be tellable apart.
    files_without_logging: int = 0

    @property
    def gating(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.gates)


# --------------------------------------------------------------------------
# Scope grouping
#
# Every rule below asks some version of "what else does this function do?",
# and `func_lineno` is what makes that exact. Grouping by `func_name` would
# merge two same-named methods on different classes into one scope, which
# this repository already has (`progress.py` holds two `_depth`s).
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
                    f"of whether this is work worth watching or plumbing — which "
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
                f"pass the caller's frame through: "
                f"`log.{site.method}(message, *args, stacklevel=2)`. One keyword, "
                f"and every call site gets its own identity back."
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
                f'use lazy %-formatting: `log.{site.method}("row %d parsed", i)` '
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
        where = (
            "this module"
            if first.func_name == static.MODULE_SCOPE
            else f"{first.func_name}()"
        )
        yield Finding(
            rule="nothing-repeating",
            pathname=structure.pathname,
            lineno=first.lineno,
            what=(
                f"{where} logs {len(sites)} times and contains no loop at all, so "
                f"nothing here repeats. No source has a period, so nothing will "
                f"draw a bar — the display can show a heartbeat and the newest "
                f"line, and that is the whole of it."
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


def _findings_for(
    structure: static.FileStructure, *, all_loops: bool = False
) -> list[Finding]:
    # The receiver-blind match in `static._MESSAGE_ARG` accepts any
    # `X.debug/info/warning/error/…` call, so `parser.error("no such file")`
    # is indistinguishable from a log call. That is the right trade for the
    # display, where a *missed* call site corrupts sibling ordinals and a
    # false one costs a pixel. It is the wrong trade here: "your
    # `parser.error` should use lazy %-formatting" is advice a user spots
    # instantly, and a linter is only worth running if it is believed.
    #
    # Measured over this venv's installed packages (issue #61): 566
    # recognised call sites, 110 of them not loggers. Requiring the file to
    # import `logging` keeps 429 of the 456 real ones and leaves 2 of the
    # 110. So the gate costs ~6% of true findings and removes 98% of the
    # false ones, which is the direction this tool has to err in.
    if not structure.imports_logging:
        return []
    scopes = _scopes(structure)
    return [
        *_silent_loops(structure, scopes, all_loops=all_loops),
        *_slow_bodies(structure),
        *_stage_announcements(structure, scopes),
        *_wrappers(structure),
        *_fstrings(structure),
        *_nothing_repeating(structure, scopes),
    ]


def check_file(pathname: str, *, all_loops: bool = False) -> tuple[Finding, ...] | None:
    """Every finding for one file, or None if it cannot be read or parsed."""
    structure = static.analyze_file(pathname)
    if structure is None:
        return None
    findings = _findings_for(structure, all_loops=all_loops)
    return tuple(sorted(findings, key=lambda finding: finding.sort_key))


# --------------------------------------------------------------------------
# Walking a tree
# --------------------------------------------------------------------------


def python_files(paths: Iterable[str]) -> list[str]:
    """Every `.py` file under `paths`, in a stable order.

    A path naming a file is taken as given whatever its suffix — someone
    pointing at one file means that file. Only directory walks filter.
    """
    found: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                )
                found.extend(
                    os.path.join(root, name)
                    for name in sorted(names)
                    if name.endswith(".py")
                )
        else:
            found.append(path)
    return found


def check(paths: Iterable[str], *, all_loops: bool = False) -> Report:
    """Analyse every file under `paths` and collect one report."""
    findings: list[Finding] = []
    files = loops = sites = repeating = silent = unlogged = 0
    for pathname in python_files(paths):
        structure = static.analyze_file(pathname)
        if structure is None:
            continue
        files += 1
        unlogged += not structure.imports_logging
        loops += len(structure.loops)
        sites += len(structure.call_sites)
        repeating += sum(1 for s in structure.call_sites.values() if s.loop_chain)
        silent += sum(1 for loop in structure.loops.values() if not loop.call_sites)
        findings.extend(_findings_for(structure, all_loops=all_loops))
    return Report(
        findings=tuple(sorted(findings, key=lambda finding: finding.sort_key)),
        files=files,
        loops=loops,
        call_sites=sites,
        repeating_call_sites=repeating,
        silent_loops=silent,
        files_without_logging=unlogged,
    )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_WIDTH: Final = 78


def _paragraph(label: str, text: str) -> str:
    return textwrap.fill(
        f"{label}{text}",
        width=_WIDTH,
        initial_indent="    ",
        subsequent_indent="    " + " " * len(label),
    )


def format_finding(finding: Finding) -> str:
    """`path:line: rule` plus the observation and the edit, wrapped."""
    lines = [
        f"{finding.pathname}:{finding.lineno}: {finding.rule}",
        _paragraph("", finding.what),
        _paragraph("fix:  ", finding.fix),
    ]
    if finding.also is not None:
        lines.append(_paragraph("also: ", finding.also))
    return "\n".join(lines)


def format_report(report: Report) -> str:
    """The whole run: findings in value order, then what was looked at."""
    blocks = [format_finding(finding) for finding in report.findings]
    unreported = report.silent_loops - sum(
        1
        for finding in report.findings
        if finding.rule in ("loop-not-logged", "loop-logs-around")
    )
    summary = [
        f"{report.files} file(s): {report.loops} loops, {report.call_sites} log "
        f"calls, {report.repeating_call_sites} of them inside a loop body.",
    ]
    if report.files_without_logging:
        summary.append(
            textwrap.fill(
                f"{report.files_without_logging} of those never import `logging`, "
                f"so nothing in them is known to be a log call and none of them "
                f"was reported on.",
                width=_WIDTH,
            )
        )
    if unreported:
        summary.append(
            textwrap.fill(
                f"{unreported} more loop(s) have no log line in the body and are "
                f"not reported: their functions log nothing at all, so nothing in "
                f"the source says whether they are work worth watching or "
                f"plumbing. Adding a line inside the ones that are slow is still "
                f"the highest-value change available — pass --all-loops to see "
                f"where they are.",
                width=_WIDTH,
            )
        )
    if not report.findings:
        summary.insert(0, "No findings.")
    return "\n\n".join([*blocks, *summary])


# --------------------------------------------------------------------------
# #58 — the agent-facing rules
#
# Debug logging is written to be deleted, and that is now substantially an
# *agent* habit: "remove the temporary debug logging" is near-universal
# tidying, and in a lumberjack codebase it strips exactly what the display is
# built from, while looking like good housekeeping.
#
# The five bold leads below are quoted verbatim from the "How to log so this
# works" section of `examples/demo.py`, which #58 names as the canonical
# wording. `tests/test_lint.py` reads that file and asserts they still match,
# so the two cannot drift. The sixth rule is #58's own addition — the demo's
# list does not carry it, which is lumberjack: see issue #65.
#
# Print-only, deliberately. Writing into someone's CLAUDE.md unasked is
# intrusive, and doing it idempotently needs more decisions than a flag —
# lumberjack: see issue #66.
# --------------------------------------------------------------------------

AGENT_RULES: Final = """\
<!-- lumberjack: how to log so the progress display works -->
## Logging conventions in this project

This project's terminal progress display (lumberjack) is built from its own
log records: which source location fires, how often, and in what order. Log
lines here are load-bearing output, not scratch work. Deleting them deletes
the display, and it looks like tidying up.

- **Leave the `logger.debug` lines in, and add more.** Density is input
  quality. Do not remove debug logging while cleaning up, and do not move a
  call to a level that is filtered out. A loop that logs once per iteration
  is a progress bar; a loop that logs nothing is invisible.
- **Log inside the body, not around it.** A line before and after a loop says
  it started and finished. A line *in* it says how fast it is going.
- **A line per phase of a slow body is worth more than one line per body.**
  Five lines in a three-second iteration can say where you are within it; one
  line can only say it happened.
- **Announce each stage of a multi-stage program.** One `log.info("stage 2:
  parsing records")` per stage names what is running now and lets the
  finished stage collapse.
- **Do not build a logging wrapper without `stacklevel=`.** Identity is the
  source location, so a shim makes every call site in the program look like
  one line. Write `log.debug(msg, *args, stacklevel=2)`.
- **Use lazy `%`-formatting in log calls, never an f-string.** Write
  `log.debug("row %d parsed", i)`, not `log.debug(f"row {i} parsed")`: lazy
  formatting keeps the template and the data in separate fields, and an
  f-string destroys the template unrecoverably. ruff's G001-G004 enforce it.

Run `python -m lumberjack.lint` to see which of these this codebase is
missing, with the line to add for each."""

#: How many specific findings the appended section names before it stops.
#: Enough to be concrete, few enough that the block stays pasteable.
AGENT_RULES_EXAMPLES: Final = 8


def format_agent_rules(report: Report | None = None) -> str:
    """The injectable block, with this codebase's own gaps when there are any.

    A generic rule list is easy to ignore; "this file has three loops with no
    body logging, at these lines" is not. That specificity is #58's whole
    argument for the linter owning this rather than a docs page.
    """
    if report is None or not report.findings:
        return AGENT_RULES
    shown = report.findings[:AGENT_RULES_EXAMPLES]
    lines = [
        AGENT_RULES,
        "",
        "### What this codebase is missing right now",
        "",
    ]
    lines.extend(
        f"- `{finding.pathname}:{finding.lineno}` — {finding.rule}" for finding in shown
    )
    remaining = len(report.findings) - len(shown)
    if remaining:
        lines.append(f"- … and {remaining} more.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

#: Findings exist and at least one of them is a defect. Chosen so the command
#: gates a build; `nothing-repeating` deliberately does not count.
EXIT_FINDINGS: Final = 1
#: Nothing to analyse. A mistyped path is a different failure from a clean
#: run, and a gate that cannot tell them apart passes when it should not.
EXIT_NO_FILES: Final = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lumberjack.lint",
        description=(
            "Report what this codebase would have to log for lumberjack's "
            "progress display to see it, and which line to add where."
        ),
        epilog=(
            "Exits 1 when there are findings to fix, 2 when there is nothing "
            "to analyse, 0 otherwise."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="files or directories to analyse (default: the current directory)",
    )
    parser.add_argument(
        "--all-loops",
        action="store_true",
        help=(
            "also report loops whose whole function logs nothing, where the "
            "source cannot tell real work from plumbing. Noisy by design, and "
            "these never affect the exit code."
        ),
    )
    parser.add_argument(
        "--agent-rules",
        action="store_true",
        help=(
            "print an injectable block for CLAUDE.md / AGENTS.md / .cursorrules "
            "telling agents not to delete the logging this display reads, with "
            "this codebase's own gaps appended. Prints only; exits 0."
        ),
    )
    args = parser.parse_args(argv)

    report = check(args.paths, all_loops=args.all_loops)
    if args.agent_rules:
        # A block meant to be piped into a file, so it goes to stdout on its
        # own and never gates: `>> CLAUDE.md` under `set -e` must not fail
        # because the findings it is describing exist.
        print(format_agent_rules(report))
        return 0

    print(format_report(report))
    if report.files == 0:
        print("Nothing to analyse.", file=sys.stderr)
        return EXIT_NO_FILES
    return EXIT_FINDINGS if report.gating else 0


if __name__ == "__main__":
    raise SystemExit(main())
