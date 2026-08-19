"""What `pytest.mark.tier2` claims, and what checks the claim.

Tier 2 is the contract tier: a component's public API, exercised through
that API. Fabricated inputs are fine — that is the line against tier 1,
which may not fabricate anything — but reaching into a component's internals
is not, because a test that asserts on private state is pinning an
implementation rather than a contract.

The mark is a claim, so it is checked. Tier 3 carries no mark and gets no
check, which is what makes drift run downward: a test written carelessly
claims nothing rather than sliding into a tier someone believes was reviewed.

Weaker than `tests/tier1/test_tier1_rules.py` on purpose. Tier 2 files stay
where they are and keep their existing shape, so the only rule is the one
that distinguishes a contract from an implementation detail.
"""

from __future__ import annotations

import ast
import subprocess  # nosec B404 - runs ruff over the marked files
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).parent
_CONFTEST = _TESTS / "conftest.py"

#: Both halves of "reaches into internals", because one of them alone was a
#: hole big enough to drive the flagship tier-2 file through. `SLF001` is
#: *attribute* access — `store._conn` — and nothing else; it says nothing
#: about `from lumberjack.store import _COLUMNS`, which is the same act
#: reached by a different syntax. `test_store.py` did exactly that and the
#: gate reported clean, while `test_schema.py` was held out of the tier for
#: committing the identical import *plus* one attribute access. The rule
#: being enforced was "no private attribute access"; the rule everyone
#: believed was being enforced is this one.
_RULES = "SLF001,PLC2701"

#: `PLC2701` is preview-gated in ruff. `--select` still bounds the run to
#: exactly these two, so preview turns nothing else on.
_RUFF = (
    "check",
    "--select",
    _RULES,
    "--isolated",
    "--preview",
    "--no-cache",
    "--output-format",
    "concise",
)


def _ruff(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "ruff", *_RUFF, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _tier2_files() -> list[Path]:
    """Every test module carrying the file-wide `tier2` mark.

    Read from the source rather than by importing: collecting via pytest
    would need a plugin, and the mark is a module-level assignment that the
    AST states plainly.

    `rglob`, so a mark placed under `tests/tier1/` is collected and checked
    rather than silently ignored — the non-recursive form left a directory
    where the mark looked applied and did nothing. `AnnAssign` as well as
    `Assign`, so `pytestmark: list = [...]` is not invisible for the sake of
    an annotation.
    """
    marked = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                targets = (
                    {node.target.id} if isinstance(node.target, ast.Name) else set()
                )
                value = node.value
            elif isinstance(node, ast.Assign):
                targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
                value = node.value
            else:
                continue
            if (
                "pytestmark" in targets
                and value is not None
                and "tier2" in ast.unparse(value)
            ):
                marked.append(path)
    return marked


def test_the_tier_is_not_empty() -> None:
    """A rule with nothing to check passes for the wrong reason. If the mark
    is ever removed everywhere, this says so rather than going quietly green."""
    assert _tier2_files(), "no file carries pytest.mark.tier2"


@pytest.mark.parametrize("path", _tier2_files(), ids=lambda p: p.name)
def test_a_tier2_file_touches_no_private_members(path: Path) -> None:
    """`SLF001` and `PLC2701` over the marked files, and `--isolated` is
    load-bearing.

    `pyproject.toml` exempts `SLF001` for `tests/**` — rightly, since tier 3
    unit-tests private functions on purpose — and `per-file-ignores` applies
    even to a CLI `--select`, so without `--isolated` this reports zero and
    proves nothing. That failure mode is not hypothetical: it was hit while
    counting these findings for the last change, and reported "all checks
    passed" over 31 of them.
    """
    result = _ruff(path)
    assert result.returncode == 0, (
        f"{path.name} is marked tier2 but reaches into private members:\n"
        f"{result.stdout}"
    )


def test_the_shared_fixtures_reach_into_exactly_one_private_thing() -> None:
    """The gap this tier has, pinned rather than left implicit.

    `conftest.py` carries no mark and is not a test, but its autouse
    fixtures run before and after every tier-2 test — so a private touch
    there happens *on the marked file's behalf*, and the per-file check
    above cannot see it. One exists, deliberately: `_reset_lumberjack_state`
    clears the ambient task contextvar, and there is no public way to do
    that. It is load-bearing rather than tidy-up — `_ambient_parent()` walks
    past handles that have *ended*, and a handle entered and never exited
    has not ended, so without the reset it would parent the next test's
    tasks.

    So the guarantee is bounded rather than absolute, and this is where the
    bound is written down: exactly one, and it is that one. A second finding
    fails here and has to be argued rather than absorbed.
    """
    result = _ruff(_CONFTEST)
    findings = [line for line in result.stdout.splitlines() if ":" in line]
    assert len(findings) == 1, f"conftest.py private access changed:\n{result.stdout}"
    assert "_current_task" in findings[0], findings[0]
