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

This is a package rather than one module because it held four separate jobs —
the rule generators, report formatting, the agent-facing rules block, and the
CLI — that had stopped sharing much beyond a name. `rules.py` holds the seven
rules and the scope-grouping they share; `report.py` holds the formatting
helpers and `AGENT_RULES`; `cli.py` holds the argparse CLI, with
`__main__.py` as the entry shim a package (unlike a single module) needs for
`python -m lumberjack.lint` to keep working. `check()`, `_report_for()`,
`Finding`, `Report` and `python_files()` stay here as the surface everything
above reads from, and this module re-exports the rest, so `from lumberjack
import lint; lint.check(...)`, `lint.Finding`, `lint.format_report(...)` and
`lint.main(...)` are exactly as they were when this was one file.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from lumberjack import static
from lumberjack.lint.report import (
    AGENT_RULES,
    AGENT_RULES_EXAMPLES,
    format_agent_rules,
    format_finding,
    format_report,
)

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
        """Rank within `RULE_ORDER`, then path, then line.

        A rule missing from `RULE_ORDER` raises `KeyError` here rather than
        sorting quietly to one end — a new rule has to state its value before
        anything will print it.
        """
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
        """The subset of `findings` that should fail a build.

        Empty while `findings` is not is a legitimate outcome rather than a
        bug: the extra loops `--all-loops` asks for are reported on request,
        and `nothing-repeating` describes a one-shot script correctly, so
        neither gates.
        """
        return tuple(finding for finding in self.findings if finding.gates)


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

# `rules.py` constructs `Finding` instances, so it imports this class from
# this module — which means it can only be imported once `Finding` exists
# above, hence this sits after the dataclasses rather than with the rest of
# the imports at the top of the file.
from lumberjack.lint.rules import (  # noqa: E402
    _fstrings,
    _nothing_repeating,
    _scopes,
    _silent_loops,
    _slow_bodies,
    _stage_announcements,
    _wrappers,
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


def _report_for(
    pathname: str, *, all_loops: bool = False
) -> tuple[static.FileStructure, tuple[Finding, ...]] | None:
    """One file's structure and its sorted findings, or None if unreadable.

    The one place `check()`'s per-file walk lives: parse, find, sort. Private
    because nothing outside this module needs one file's report on its own —
    `check()` is the public surface, over any number of paths, and folds this
    into the totals it also has to compute from `structure`.
    """
    structure = static.analyze_file(pathname)
    if structure is None:
        return None
    findings = _findings_for(structure, all_loops=all_loops)
    return structure, tuple(sorted(findings, key=lambda finding: finding.sort_key))


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
        # A walked file is reported the way `Path` spells it, so `./src` comes
        # back as `src/x.py`. A named file is appended exactly as given.
        top = Path(path)
        if top.is_dir():
            for parent, dirs, names in top.walk():
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                )
                found.extend(
                    str(parent / name) for name in sorted(names) if name.endswith(".py")
                )
        else:
            found.append(path)
    return found


def check(paths: Iterable[str], *, all_loops: bool = False) -> Report:
    """Analyse every file under `paths` and collect one report."""
    findings: list[Finding] = []
    files = loops = sites = repeating = silent = unlogged = 0
    for pathname in python_files(paths):
        result = _report_for(pathname, all_loops=all_loops)
        if result is None:
            continue
        structure, file_findings = result
        files += 1
        unlogged += not structure.imports_logging
        loops += len(structure.loops)
        sites += len(structure.call_sites)
        repeating += sum(1 for s in structure.call_sites.values() if s.loop_chain)
        silent += sum(1 for loop in structure.loops.values() if not loop.call_sites)
        findings.extend(file_findings)
    return Report(
        findings=tuple(sorted(findings, key=lambda finding: finding.sort_key)),
        files=files,
        loops=loops,
        call_sites=sites,
        repeating_call_sites=repeating,
        silent_loops=silent,
        files_without_logging=unlogged,
    )


# The CLI lives in `cli.py`, not `__main__.py`, precisely so this re-export
# can be an ordinary import: importing `__main__.py` from here would register
# it in sys.modules and make `python -m lumberjack.lint` warn about running a
# module that was already imported. See `cli.py`'s docstring. Imported after
# `check()` above because `cli.py` reads it back from this package.
from lumberjack.lint.cli import (  # noqa: E402
    EXIT_FINDINGS,
    EXIT_NO_FILES,
    main,
)

__all__ = [
    "AGENT_RULES",
    "AGENT_RULES_EXAMPLES",
    "EXIT_FINDINGS",
    "EXIT_NO_FILES",
    "RULE_ORDER",
    "Finding",
    "Report",
    "check",
    "format_agent_rules",
    "format_finding",
    "format_report",
    "main",
    "python_files",
]
