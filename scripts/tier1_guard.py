"""Refuse a pull request that edits tier 1 and `src/` in the same diff.

Tier 1 is the acceptance contract, and `tests/README.md` says a failing
tier-1 test means the change is wrong rather than the test. That is an
instruction, and an instruction is the weakest of the three things available
here — instruct, gate the merge, detect after the fact. This is the middle
one: the signature of the failure mode it exists to catch is a diff that
changes behaviour under `src/` and, in the same breath, edits the assertion
that noticed.

Editing tier 1 is not forbidden. It is made *visible*, by requiring a label
that only someone with triage rights on the repository can apply.

## What this does not catch, stated exactly

Three gaps, and the third used to be described here as impossible:

1. A pull request that weakens a tier-1 assertion and touches nothing under
   `src/`. Rarer and purer, and no in-repo mechanism stops it.
2. A rename *into* `tests/tier1/` of a file that was already weak. The diff
   is read as paths, never as content.
3. **A pull request that rewrites `.github/workflows/tier1-guard.yml`
   itself.** A `pull_request` workflow runs from the pull request's own
   merge ref, so GitHub executes the *proposed* workflow file rather than
   trunk's. Nothing in this repository can change that, and no label is
   needed to do it. What is bounded is the blast radius: the workflow is one
   file, it is listed in `PROTECTED` below so an *unmodified* guard refuses
   a diff touching it alongside `src/`, and editing it is loud in review in
   a way that editing a helper under `scripts/` is not.

Gap 3 is why this file is no longer executed from the workspace. The
workflow reads the **base branch's** copy (`git show "$BASE_SHA:…"`) and
runs that, so a diff which edits this script cannot appoint itself judge.
That was a real bypass rather than a hypothetical: the decider, its tests
and the workflow's own `run:` block all sit outside `tests/tier1/`, so a
diff could change `src/`, hollow out a tier-1 assertion, and replace this
file with one that exits 0 — and the required check went green.

Run by `.github/workflows/tier1-guard.yml`, which pipes the pull request's
changed paths in on stdin and passes its labels in the environment rather
than on the command line — a label is attacker-controlled text, and
interpolating one into a shell command is how a workflow gets hijacked.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable

#: Paths whose edits are gated.
#:
#: Prefixes rather than globs: `tests/tier1/` is a directory by design, and
#: `test_tier1_rules.py` lives inside it precisely so that weakening the
#: rules is itself a tier-1 edit. The other two are the guard's own moving
#: parts — a diff that edits the decider or the workflow *and* `src/` is the
#: same signature as one that edits an assertion and `src/`, and used to be
#: the way through this check rather than something it noticed.
PROTECTED = (
    "tests/tier1/",
    "scripts/tier1_guard.py",
    ".github/workflows/tier1-guard.yml",
)

#: What makes a gated edit suspicious rather than routine.
IMPLEMENTATION = "src/"

#: Applying this needs triage rights, which is the whole mechanism.
LABEL = "tier-1 change"


def read_paths(raw: str) -> list[str]:
    """The changed paths, from whichever separator git used.

    NUL-separated is what the workflow asks for (`git diff -z`), because it
    is the only form git never rewrites. With newline separators and git's
    default `core.quotePath`, a path holding a non-ASCII byte comes back
    C-quoted — `"tests/tier1/test_\\303\\274nicode.py"` — which starts with a
    quote rather than with `tests/`, so every prefix test below returns
    False and the diff is waved through. That is the one direction this must
    not fail in, so a path that still arrives quoted raises rather than
    being guessed at. Newlines stay supported for running it by hand.
    """
    parts = raw.split("\0") if "\0" in raw else raw.splitlines()
    paths = [p.strip() for p in parts if p.strip()]
    if any(p.startswith('"') for p in paths):
        message = (
            "Refusing to read a quoted path: git rewrote at least one name, "
            "so a prefix test cannot be trusted. The workflow passes `-z` to "
            "stop that happening."
        )
        raise ValueError(message)
    return paths


def is_gated(paths: Iterable[str]) -> bool:
    """Whether this diff is the shape the guard exists for.

    Separate from `verdict()` so that an *allowed* run can still say which
    of the two reasons allowed it. "Nothing gated here" and "gated, and
    somebody signed for it" are different facts, and a green check that
    reports the first when the second happened is the display lying about
    what it knows.
    """
    paths = list(paths)
    return any(p.startswith(PROTECTED) for p in paths) and any(
        p.startswith(IMPLEMENTATION) for p in paths
    )


def verdict(paths: Iterable[str], labels: Iterable[str]) -> str | None:
    """The refusal text, or `None` to allow.

    Returning the message rather than printing it keeps the decision testable
    without capturing output, and keeps the wording in one place.
    """
    paths = list(paths)
    if not is_gated(paths):
        return None
    protected = sorted(p for p in paths if p.startswith(PROTECTED))
    implementation = sorted(p for p in paths if p.startswith(IMPLEMENTATION))
    if any(label.casefold() == LABEL.casefold() for label in labels):
        return None
    headline = f"This pull request edits the acceptance contract and {IMPLEMENTATION}"
    return "\n".join(
        [
            f"{headline} together.",
            "",
            "  contract:",
            *(f"    {p}" for p in protected),
            f"  {IMPLEMENTATION}",
            *(f"    {p}" for p in implementation),
            "",
            "Tier 1 is the acceptance contract: a failing tier-1 test means the",
            "change is wrong, not the test. Two ways forward, and only the first",
            "is available from inside the repository:",
            "",
            "  1. Revert the contract edits and fix the change instead.",
            "  2. If the test is genuinely wrong, say why in the pull request,",
            "     move the edit into its own commit with its own argument, and",
            f"     ask someone with triage rights for the {LABEL!r} label.",
        ]
    )


def main() -> int:
    """Read the diff from stdin and the labels from `PR_LABELS`."""
    try:
        paths = read_paths(sys.stdin.read())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    labels = _labels(os.environ.get("PR_LABELS", ""))
    refusal = verdict(paths, labels)
    if refusal is None:
        if is_gated(paths):
            print(f"Contract edit, allowed by the {LABEL!r} label.")
        else:
            print(f"No contract edit alongside {IMPLEMENTATION}: {len(paths)} changed.")
        return 0
    print(refusal, file=sys.stderr)
    return 1


def _labels(raw: str) -> list[str]:
    """The label names GitHub passed, or none.

    Unparseable input degrades to *no* labels, which is the strict reading —
    a guard whose failure mode is to wave a change through would be worse
    than no guard, so the one direction this may fail in is closed.
    """
    try:
        loaded = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, str)]


if __name__ == "__main__":
    raise SystemExit(main())
