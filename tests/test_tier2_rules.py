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


def _tier2_files() -> list[Path]:
    """Every test module carrying the file-wide `tier2` mark.

    Read from the source rather than by importing: collecting via pytest
    would need a plugin, and the mark is a module-level assignment that the
    AST states plainly.
    """
    marked = []
    for path in sorted(_TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "pytestmark" in targets and "tier2" in ast.unparse(node.value):
                marked.append(path)
    return marked


def test_the_tier_is_not_empty() -> None:
    """A rule with nothing to check passes for the wrong reason. If the mark
    is ever removed everywhere, this says so rather than going quietly green."""
    assert _tier2_files(), "no file carries pytest.mark.tier2"


@pytest.mark.parametrize("path", _tier2_files(), ids=lambda p: p.name)
def test_a_tier2_file_touches_no_private_members(path: Path) -> None:
    """SLF001 over the marked files, and `--isolated` is load-bearing.

    `pyproject.toml` exempts `SLF001` for `tests/**` — rightly, since tier 3
    unit-tests private functions on purpose — and `per-file-ignores` applies
    even to a CLI `--select`, so without `--isolated` this reports zero and
    proves nothing. That failure mode is not hypothetical: it was hit while
    counting these findings for the last change, and reported "all checks
    passed" over 31 of them.
    """
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            str(path),
            "--select",
            "SLF001",
            "--isolated",
            "--no-cache",
            "--output-format",
            "concise",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{path.name} is marked tier2 but reaches into private members:\n"
        f"{result.stdout}"
    )
