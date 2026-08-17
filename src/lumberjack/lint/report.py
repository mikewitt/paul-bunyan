"""Turning findings into text: the human report and the agent-facing block.

`lint.py` used to be one module holding four separate jobs; see the
package's `__init__.py` for the four-way split this file is one quarter of.
Nothing here calls `static.analyze_file()` or constructs a `Finding` — it
only formats ones `rules.py` already produced and `check()` already
collected. `Finding`/`Report` are referenced only in type annotations, which
`from __future__ import annotations` defers, so the import of them below is
`TYPE_CHECKING`-only and this module carries no runtime dependency on the
package `__init__` that would need import-order care.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from lumberjack.lint import Finding, Report

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
