"""Launching a real child process, shared by the suites that need one.

Named `script_runner` rather than anything starting with `subprocess`:
bandit's B404 matches the *module name* by prefix, so `subprocess_rig` made
every `from subprocess_rig import ...` line a security finding — seven of
them, none of which imports `subprocess` at all. Suppressing those would
have put `# nosec B404` on seven lines that would then each claim something
untrue. The name is also more accurate; this runs scripts.

Extracted from `test_exit_paths.py` when `tests/tier1/` started launching
the same way. A plain module rather than `conftest.py` for the reason
`fixture_sources.py` and `rich_rig.py` already establish: these are helpers
imported by name, not fixtures, and nothing here should run for every test
in the suite.

`ANSI_RE`'s `?` is load-bearing and travels with it — see its comment.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess  # nosec B404 - these tests launch real child processes
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent

# The `?` matters: these are *negative* assertions, and without it a
# private-mode sequence such as hide-cursor (`\x1b[?25l`) slips through
# unmatched — the exact drift the conftest `strip_ansi` note records.
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")

#: Some scripts ask for `output_mode="rich"` and then assert on what only the
#: *lossy* live bar does. On a bare install the factory hands back the
#: write-through plain renderer instead, which correctly prints everything, so
#: the assertions would be measuring the fallback rather than the bar.
#:
#: Apply it to the narrowest test that needs it. A test asserting what
#: survives *either* renderer — a warning getting through, no cursor control
#: on a pipe — is a Principle 9 degradation check and must stay unmarked, or
#: the `bare install (no rich)` job silently stops running it.
needs_rich = pytest.mark.skipif(
    importlib.util.find_spec("rich") is None,
    reason="asserts live-bar behaviour, which degrades to plain without rich",
)


def run_script(
    scripts_dir: Path,
    name: str,
    subprocess_env: Callable[..., dict[str, str]],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run `name` from `scripts_dir` in its own interpreter, captured."""
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(scripts_dir / name)],
        capture_output=True,
        env=subprocess_env(env),
        timeout=30,
    )


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a child process needs to exercise lumberjack from src.

    A plain function rather than only a fixture, because the callers are not
    all function-scoped. A module-scoped fixture cannot request a
    function-scoped one, and `test_exit_paths.py` used to resolve that by
    restating these five lines with a docstring apologising for it. Two
    copies were tolerable; the tier-1 suite would have made three.

    Two of the lines are non-obvious. `COVERAGE_PROCESS_START` makes
    pytest-cov's `.pth` hook measure the child — every subprocess test's
    coverage is lost without it, silently, because the run still passes.
    `PYTHONIOENCODING` pins the child's stdio so the display's `…` and `━`
    survive a Windows default of cp1252.
    """
    full_env = dict(os.environ)
    # The child's lumberjack configuration comes from the test, never from
    # whoever's shell is running it. `piped_is_plain.py` deliberately omits
    # `output_mode` so that *detection* decides — and an inherited
    # `LUMBERJACK_OUTPUT_MODE=plain` would satisfy it through the override
    # branch instead, passing for the wrong reason and silently. `=rich`
    # would fail loudly, so only the useless direction was quiet. `extra`
    # is applied below, so a test that wants one still gets it.
    for key in [k for k in full_env if k.startswith("LUMBERJACK_")]:
        del full_env[key]
    full_env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT / "src"), full_env.get("PYTHONPATH", "")]
    )
    full_env["PYTHONIOENCODING"] = "utf-8"
    full_env["COVERAGE_PROCESS_START"] = str(_REPO_ROOT / "pyproject.toml")
    if extra:
        full_env.update(extra)
    return full_env
