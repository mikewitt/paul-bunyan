"""Refuse a pull request that edits tier 1 and `src/` in the same diff.

Tier 1 is the acceptance contract, and `tests/README.md` says a failing
tier-1 test means the change is wrong rather than the test. That is an
instruction, and an instruction is the weakest of the three things available
here — instruct, gate the merge, detect after the fact. This is the middle
one: the signature of the failure mode it exists to catch is a diff that
changes behaviour under `src/` and, in the same breath, edits the assertion
that noticed.

Editing tier 1 is not forbidden. It is made *visible*, by requiring a label
that only someone with triage rights on the repository can apply — so the
one way past this guard is outside the working tree, which is the point. An
agent can write any file it likes; it cannot label its own pull request.

What this deliberately does not catch, so nobody mistakes it for complete: a
pull request that weakens a tier-1 assertion and touches nothing under
`src/`. That is rarer and purer, and no in-repo mechanism stops it.

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

#: Paths whose edits are gated. A prefix rather than a glob: `tests/tier1/`
#: is a directory by design, and `test_tier1_rules.py` lives inside it
#: precisely so that weakening the rules is itself a tier-1 edit.
PROTECTED = "tests/tier1/"

#: What makes a tier-1 edit suspicious rather than routine.
IMPLEMENTATION = "src/"

#: Applying this needs triage rights, which is the whole mechanism.
LABEL = "tier-1 change"


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
    return "\n".join(
        [
            f"This pull request edits tier 1 and {IMPLEMENTATION} together.",
            "",
            "  tier 1:",
            *(f"    {p}" for p in protected),
            f"  {IMPLEMENTATION}",
            *(f"    {p}" for p in implementation),
            "",
            "Tier 1 is the acceptance contract: a failing tier-1 test means the",
            "change is wrong, not the test. Two ways forward, and only the first",
            "is available from inside the repository:",
            "",
            f"  1. Revert the {PROTECTED} edits and fix the change instead.",
            "  2. If the test is genuinely wrong, say why in the pull request,",
            "     move the edit into its own commit with its own argument, and",
            f"     ask someone with triage rights for the {LABEL!r} label.",
        ]
    )


def main() -> int:
    """Read the diff from stdin and the labels from `PR_LABELS`."""
    paths = [line.strip() for line in sys.stdin if line.strip()]
    labels = _labels(os.environ.get("PR_LABELS", ""))
    refusal = verdict(paths, labels)
    if refusal is None:
        if is_gated(paths):
            print(f"Tier-1 edit, allowed by the {LABEL!r} label.")
        else:
            print(f"No tier-1 edit alongside {IMPLEMENTATION}: {len(paths)} changed.")
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
