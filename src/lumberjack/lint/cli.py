"""The CLI behind `python -m lumberjack.lint [PATH ...]`.

`lint.py` used to be one module and ran its CLI off `if __name__ ==
"__main__":`; see the package's `__init__.py` for the four-way split this
file is one quarter of. The CLI lives here rather than in `__main__.py`
because the package `__init__.py` re-exports `main`, `EXIT_FINDINGS` and
`EXIT_NO_FILES` — so `lint.main(...)` works as it did when this was one
module — and importing them *from* `__main__.py` would register that module
in `sys.modules` as a side effect of `import lumberjack.lint`, at which point
`python -m lumberjack.lint` makes `runpy` warn about running a module that
was already imported, and `__main__.py` runs twice as two module objects.
`__main__.py` is therefore only the two-line entry shim over this module.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from lumberjack.lint import check
from lumberjack.lint.report import format_agent_rules, format_report

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
