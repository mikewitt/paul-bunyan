"""The lint hooks and the lint CI job must enforce the same ruleset.

`.pre-commit-config.yaml` pins tool versions by git rev; `uv sync` resolves
them from `pyproject.toml`. Nothing links the two, so they drift — and the
symptom is the worst kind: a commit that passes `pre-commit` locally and
fails the `lint` job in CI, over a rule one of the two versions doesn't have.

A test rather than a CI-only script, so the answer is the same before the
push as after it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_CONFIG = Path(__file__).resolve().parent.parent / ".pre-commit-config.yaml"

#: pre-commit repo URL -> the console script whose --version we compare against.
_PINNED_TOOLS = {
    "https://github.com/astral-sh/ruff-pre-commit": "ruff",
    "https://github.com/psf/black": "black",
}


def _pinned_revs() -> dict[str, str]:
    """Map each pre-commit repo URL to its pinned rev, leading `v` stripped."""
    text = _CONFIG.read_text(encoding="utf-8")
    pairs = re.findall(
        r"^\s*-\s*repo:\s*(\S+)\s*\n\s*rev:\s*(\S+)\s*$", text, re.MULTILINE
    )
    return {repo: rev.lstrip("v") for repo, rev in pairs}


def _installed_version(tool: str) -> str:
    """The version `uv sync` actually resolved, from the tool's own output."""
    out = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", tool, "--version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.search(r"(\d+\.\d+\.\d+)", out)
    if match is None:
        raise AssertionError(f"no version in {tool} --version output: {out!r}")
    return match.group(1)


def test_config_pins_every_tool_we_check() -> None:
    """Guards the test below: a renamed repo URL must fail loudly, not pass."""
    assert set(_pinned_revs()) == set(_PINNED_TOOLS)


@pytest.mark.parametrize(("repo", "tool"), sorted(_PINNED_TOOLS.items()))
def test_hook_pin_matches_the_installed_version(repo: str, tool: str) -> None:
    pinned = _pinned_revs()[repo]
    installed = _installed_version(tool)
    assert pinned == installed, (
        f"{tool}: .pre-commit-config.yaml pins {pinned}, but uv resolved "
        f"{installed}. The hook and the CI lint job would enforce different "
        f"rules. Bump the rev in .pre-commit-config.yaml to v{installed} "
        f"(ruff) / {installed} (black), or pin the dependency to match."
    )
