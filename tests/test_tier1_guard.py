"""The guard that makes a tier-1 edit visible, driven directly.

`scripts/tier1_guard.py` is the merge gate behind `tests/README.md`'s rule
that a failing tier-1 test means the change is wrong rather than the test.
It shares a failure shape with `test_workflows.py`'s subject: a guard that
waves everything through is indistinguishable from a guard that is working,
because both are green. So the cases here are mostly *refusals* — the thing
that has to keep happening — and the one direction it is allowed to fail in
is closed rather than open.

The decision lives in `verdict()`, a pure function over a list of paths and
a list of labels, so none of this needs a pull request, a token or a diff.
What it cannot check is that the workflow calls it correctly; the last two
tests read `tier1-guard.yml` for that, which is the same reasoning that put
`test_workflows.py` next door.
"""

from __future__ import annotations

import importlib.util
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "tier1_guard.py"
_WORKFLOW = _ROOT / ".github" / "workflows" / "tier1-guard.yml"

_SRC = "src/lumberjack/session.py"
_TIER1 = "tests/tier1/test_display_shapes.py"


@pytest.fixture(scope="module")
def guard() -> Any:
    """Loaded by path, as `test_record_demo.py` loads the recorder."""
    spec = importlib.util.spec_from_file_location("tier1_guard", _SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        pytest.fail(f"could not load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_src_only_change_is_waved_through(guard: Any) -> None:
    assert guard.verdict([_SRC, "README.md"], []) is None


def test_a_tier1_only_change_is_waved_through(guard: Any) -> None:
    """Deliberately allowed, and the purest form of the attack.

    A pull request that hollows out a tier-1 assertion and touches nothing
    else passes this guard. It is documented as uncovered rather than half
    caught: gating every tier-1 edit would escalate the routine ones too, and
    a gate everyone learns to click past stops being a gate.
    """
    assert guard.verdict([_TIER1], []) is None


def test_the_two_together_are_refused(guard: Any) -> None:
    refusal = guard.verdict([_SRC, _TIER1], [])
    assert refusal is not None
    assert _SRC in refusal and _TIER1 in refusal


def test_the_label_is_the_way_past(guard: Any) -> None:
    assert guard.verdict([_SRC, _TIER1], ["tier-1 change"]) is None


def test_the_label_matches_regardless_of_case(guard: Any) -> None:
    """GitHub keeps a label's case and treats two that differ only by it as
    one, so a near-miss here would be a rule nobody could satisfy."""
    assert guard.verdict([_SRC, _TIER1], ["Tier-1 Change"]) is None


def test_some_other_label_does_not_open_it(guard: Any) -> None:
    assert guard.verdict([_SRC, _TIER1], ["tests", "enhancement"]) is not None


def test_the_rules_file_is_inside_what_it_protects(guard: Any) -> None:
    """`test_tier1_rules.py` lives in `tests/tier1/` so that weakening the
    rules is itself a gated edit. If it ever moves, this stops being true."""
    assert (_ROOT / "tests" / "tier1" / "test_tier1_rules.py").exists()
    assert guard.verdict([_SRC, "tests/tier1/test_tier1_rules.py"], []) is not None


@pytest.mark.parametrize(
    "raw", ["", "not json", "{}", '"a string"', "null", "[1, 2]", '["ok", 3]']
)
def test_unreadable_labels_are_read_as_none_of_them(guard: Any, raw: str) -> None:
    """The one direction this may fail in is closed.

    Every malformed input yields no *usable* label, so the guard gets
    stricter rather than more permissive — a gate that opens when its input
    is garbled would be worse than no gate.
    """
    assert "tier-1 change" not in guard._labels(raw)


def test_running_it_end_to_end_refuses_and_says_why() -> None:
    """The path the workflow actually takes: paths on stdin, labels in the
    environment, verdict in the exit code."""
    done = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(_SCRIPT)],
        input=f"{_SRC}\n{_TIER1}\n",
        env={"PR_LABELS": "[]", "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert "tier-1 change" in done.stderr


def test_running_it_end_to_end_accepts_when_labelled() -> None:
    done = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(_SCRIPT)],
        input=f"{_SRC}\n{_TIER1}\n",
        env={"PR_LABELS": '["tier-1 change"]', "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "allowed by" in done.stdout, done.stdout


def test_it_says_which_of_the_two_reasons_let_it_pass(guard: Any) -> None:
    """A green check that cannot tell "nothing gated" from "gated, and
    somebody signed for it" is reporting less than it knows."""
    assert guard.is_gated([_SRC, _TIER1])
    assert not guard.is_gated([_SRC])
    assert not guard.is_gated([_TIER1])


def test_the_workflow_reruns_when_a_label_changes() -> None:
    """Without `labeled`, applying the label would not re-run the check, and
    the way past the gate would be to push an empty commit afterwards."""
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # `on` is YAML 1.1's boolean true, which is why this is not `["on"]`.
    triggers = workflow[True]["pull_request"]["types"]
    assert {"labeled", "unlabeled", "synchronize"} <= set(triggers)


def test_the_workflow_passes_labels_through_the_environment() -> None:
    """Not into the shell. A label is text anyone with triage rights writes,
    and `${{ }}` splices it in before bash sees a quote."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "PR_LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}" in text
    run = text.split("run: |", 1)[1]
    assert "${{" not in run, "the run: block interpolates something"
    assert "scripts/tier1_guard.py" in run
