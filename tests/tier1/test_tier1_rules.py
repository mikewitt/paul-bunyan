"""What makes `tests/tier1/` a tier rather than a directory.

The rules below are the whole mechanism. Tier 1 is the acceptance contract —
the thing a reviewer reads to learn the API, and the thing a future change is
measured against — so what may appear in it is checked rather than trusted.
`tests/README.md` states the tiers in prose; this file is what enforces them.

Deliberately a test rather than lint config. Ruff's `per-file-ignores` can
only *subtract* rules from a path, never add them, so scoping an import
allowlist or a private-access ban to one directory would mean enabling it
everywhere and excluding it in four glob patterns that rot. `test_toolchain.py`
sets the precedent: a drift check whose answer must be the same before the
push as after it belongs in the suite.

This file lives inside `tests/tier1/` on purpose. Anything guarding the tier
must sit behind the same protection as the tier, or weakening the guard
becomes the cheap way in.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_TIER1 = Path(__file__).parent
_SCRIPTS = _TIER1 / "scripts"

#: What a scenario script may import. `lumberjack` bare only — a submodule
#: import means reaching past the public surface, which is what tier 2 is for.
_ALLOWED_ROOTS = frozenset({"lumberjack"}) | frozenset(sys.stdlib_module_names)

#: Calls that start a child process, and the helpers that give it an
#: environment which keeps coverage measuring it.
_LAUNCHERS = frozenset({"run_script", "subprocess.run"})
_ENV_HELPERS = frozenset({"child_env", "subprocess_env"})


def _argument_names(call: ast.Call) -> set[str]:
    """Every name this call passes, whether handed over bare or invoked.

    `run_script(..., child_env)` passes it bare; `subprocess.run(..., env=child_env())`
    invokes it. Both count.
    """
    names: set[str] = set()
    for value in [*call.args, *(kw.value for kw in call.keywords)]:
        names.add(_called_name(value))
        if isinstance(value, ast.Call):
            names.add(_called_name(value.func))
    return names


def _called_name(func: ast.expr) -> str:
    """`run_script` or `subprocess.run`, as written at the call site."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def _modules(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.py") if p.name != "__init__.py")


def _parents() -> list[Path]:
    return sorted(p for p in _TIER1.glob("test_*.py"))


def _imported_names(tree: ast.AST) -> list[str]:
    """Every module named by an import, as written.

    A relative import comes back as `.` repeated — it names no module the
    allowlist could contain, which is the point. Skipping them instead (an
    earlier version tested `node.level == 0`) left a hole: `from . import x`
    named nothing, matched nothing, and passed every check. It would fail at
    runtime here, since neither directory is a package, but a guard that
    relies on the thing it guards being broken anyway is not a guard.
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append("." * node.level + (node.module or ""))
    return names


def _private_attributes(tree: ast.AST) -> list[str]:
    return [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not node.attr.startswith("__")
    ]


@pytest.mark.parametrize("path", _modules(_SCRIPTS), ids=lambda p: p.name)
def test_a_scenario_script_imports_only_the_public_api(path: Path) -> None:
    """A script is documentation-grade user code, so it may use what a user
    can: `lumberjack` and the standard library. A submodule import (say
    `lumberjack.store`) is the tell that a script has started reaching for
    something the published surface does not offer — which is a finding about
    the API, not a licence to reach."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        name
        for name in _imported_names(tree)
        if name.split(".")[0] not in _ALLOWED_ROOTS
    ]
    assert offenders == [], f"{path.name} imports {offenders}"
    submodules = [n for n in _imported_names(tree) if n.startswith("lumberjack.")]
    assert submodules == [], f"{path.name} reaches past the public API: {submodules}"


@pytest.mark.parametrize("path", _modules(_SCRIPTS), ids=lambda p: p.name)
def test_a_scenario_script_touches_nothing_private(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _private_attributes(tree) == [], path.name


@pytest.mark.parametrize("path", _parents(), ids=lambda p: p.name)
def test_a_parent_test_never_imports_lumberjack(path: Path) -> None:
    """The rule the tier rests on. A parent observes a child process through
    its stdout, stderr, exit code and files — nothing else. The moment one
    imports the package it can quietly become an in-process test wearing
    acceptance clothing, asserting on objects rather than on what a user
    would see, and the tier stops meaning anything."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = [n for n in _imported_names(tree) if n.split(".")[0] == "lumberjack"]
    assert imported == [], f"{path.name} imports {imported}"


@pytest.mark.parametrize("path", _parents(), ids=lambda p: p.name)
def test_a_parent_test_touches_nothing_private(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _private_attributes(tree) == [], path.name


@pytest.mark.parametrize("path", _parents(), ids=lambda p: p.name)
def test_a_parent_test_does_not_monkeypatch(path: Path) -> None:
    """Black box means the child is configured through public arguments and
    the environment. A `monkeypatch` here would be reaching into the parent's
    own interpreter to change what the test observes."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    args = [
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for arg in node.args.args
    ]
    assert "monkeypatch" not in args, path.name


@pytest.mark.parametrize("path", _parents(), ids=lambda p: p.name)
def test_a_parent_test_launches_through_the_shared_rig(path: Path) -> None:
    """`child_env()` is what sets `COVERAGE_PROCESS_START`, and a parent that
    builds its own environment stops measuring the child — silently, because
    the run still passes and only the coverage number moves.

    Checked per *call*, not per file. A file-level check ("the name appears
    somewhere") passes a file whose second launch quietly builds its own
    environment while its first is correct — found by writing exactly that
    and watching the check stay green. Reading the AST also means the name
    has to be passed, not merely mentioned in a comment.

    A file that launches nothing never reaches the assertion, which is why
    `test_tier1_rules.py` needs no special-casing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node.func) not in _LAUNCHERS:
            continue
        assert _argument_names(node) & _ENV_HELPERS, (
            f"{path.name}:{node.lineno} launches a child process without "
            f"one of {sorted(_ENV_HELPERS)}"
        )


# Throwaway edit: proving the tier-1 guard blocks. Revert before merging.
