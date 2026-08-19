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
import os
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


def test_the_job_name_is_the_one_the_ruleset_lists() -> None:
    """Frozen, for the reason CLAUDE.md records for `bare install (no rich)`.

    `name:` is the check name GitHub reports, and trunk's required-check
    ruleset matches by that string. Renaming the job orphans the required
    check: the ruleset waits forever for a context nothing will ever post,
    and every merge blocks until someone edits repository settings. Nothing
    in the tree noticed, so a rename passed the whole suite.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["jobs"]["guard"]["name"] == "tier-1 guard"


def test_the_workflow_runs_the_base_branch_copy_of_the_decider() -> None:
    """Not the workspace copy, which is the pull request's own.

    A `pull_request` event checks out `refs/pull/N/merge`, so every file in
    the workspace is the proposed one. `scripts/` was not protected, so a
    diff could edit `src/`, hollow a tier-1 assertion, and replace the
    decider with one that exits 0 — and the required check went green.
    """
    run = _run_block()
    assert 'git show "$BASE_SHA:scripts/tier1_guard.py"' in run
    assert '| python "$RUNNER_TEMP/tier1_guard.py"' in run
    assert "| python scripts/tier1_guard.py" not in run


def test_the_diff_is_asked_for_in_the_two_forms_that_cannot_hide_a_path() -> None:
    """`-z` and `--no-renames`, each closing a measured fail-open.

    Without `-z`, git C-quotes a non-ASCII path and it stops starting with
    `tests/`. Without `--no-renames`, a `git mv` out of `tests/tier1/` is
    reported as its destination alone.
    """
    run = _run_block()
    assert "git diff --name-only -z --no-renames" in run


def _run_block() -> str:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["guard"]["steps"]
    return str(steps[-1]["run"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603  # nosec B603
        ["git", *args],  # noqa: S607  # nosec B607
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _scratch_repo(
    root: Path,
    changes: dict[str, str | None],
    base_changes: dict[str, str | None] | None = None,
) -> Path:
    """A repo shaped like what `pull_request` checks out.

    Base commit, a branch applying `changes`, then a merge commit — so HEAD
    has the base tip as parent 1 and the branch as parent 2, exactly as
    `refs/pull/N/merge` does. `None` as a value deletes the path, which is
    how a rename's source side arrives.

    `base_changes` land on the base branch *after* the pull request's own
    `base.sha` was recorded, which is how a base branch moves while a pull
    request is open — and the case the three-dot diff got wrong.
    """
    repo = root / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "base")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    for path, body in (
        ("src/mod.py", "VALUE = 1\n"),
        ("tests/tier1/test_contract.py", "def test_it():\n    assert VALUE == 1\n"),
        ("scripts/tier1_guard.py", _SCRIPT.read_text(encoding="utf-8")),
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(  # nosec B603
        ["git", "rev-parse", "HEAD"],  # noqa: S607  # nosec B607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "BASE_SHA").write_text(base, encoding="utf-8")

    _git(repo, "checkout", "-q", "-b", "pr")
    _write(repo, changes)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr")

    _git(repo, "checkout", "-q", "base")
    if base_changes:
        _write(repo, base_changes)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "someone else")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    return repo


def _write(repo: Path, changes: dict[str, str | None]) -> None:
    for path, body in changes.items():
        target = repo / path
        if body is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _drive_the_workflow(repo: Path, tmp_path: Path, labels: str = "[]") -> int:
    """Run the workflow's own `run:` block, verbatim, against `repo`."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    done = subprocess.run(  # noqa: S603  # nosec B603
        ["bash", "-c", _run_block()],  # noqa: S607  # nosec B607
        cwd=repo,
        env={
            "PATH": os.environ.get("PATH", ""),
            "BASE_SHA": (repo / "BASE_SHA").read_text(encoding="utf-8").strip(),
            "RUNNER_TEMP": str(runner_temp),
            "PR_LABELS": labels,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


_HOLLOWED = "def test_it():\n    assert True\n"
_SURRENDERS = "import sys\n\n\ndef main():\n    return 0\n\n\nsys.exit(0)\n"


def test_a_diff_that_rewrites_the_decider_does_not_get_to_judge_itself(
    tmp_path: Path,
) -> None:
    """The bypass this whole change exists for, end to end.

    Before: the workflow ran the workspace copy, so this diff — `src/`
    changed, the tier-1 assertion hollowed out, the decider replaced with
    one that exits 0 — reported success on the required check.
    """
    repo = _scratch_repo(
        tmp_path,
        {
            "src/mod.py": "VALUE = 2\n",
            "tests/tier1/test_contract.py": _HOLLOWED,
            "scripts/tier1_guard.py": _SURRENDERS,
        },
    )
    assert _drive_the_workflow(repo, tmp_path) == 1


def test_a_rename_out_of_tier1_still_shows_the_side_it_left(
    tmp_path: Path,
) -> None:
    """`git mv` reports one path under `--name-only`, and it is the
    destination — so the gated side of the move disappears. `--no-renames`
    asks for the delete and the add separately."""
    repo = _scratch_repo(
        tmp_path,
        {
            "src/mod.py": "VALUE = 2\n",
            "tests/tier1/test_contract.py": None,
            "tests/test_contract.py": "def test_it():\n    assert VALUE == 1\n",
        },
    )
    assert _drive_the_workflow(repo, tmp_path) == 1


def test_the_workflow_is_not_accused_of_edits_the_base_branch_made(
    tmp_path: Path,
) -> None:
    """Parent 1 of the merge ref is the base tip, so diffing against it
    reports this pull request and nothing else.

    `$BASE_SHA...HEAD` walks back to the merge base instead, so a gated pair
    that landed on the *base* branch while this pull request was open is
    read as this pull request's work and refused. Measured on the real
    thing: 18 files against the pull request's own 16.
    """
    repo = _scratch_repo(
        tmp_path,
        {"README.md": "unrelated\n"},
        base_changes={
            "tests/tier1/test_contract.py": _HOLLOWED,
            "src/mod.py": "VALUE = 3\n",
        },
    )
    assert _drive_the_workflow(repo, tmp_path) == 0


def test_a_quoted_path_is_refused_rather_than_read(guard: Any) -> None:
    """git's default `core.quotePath` C-quotes a non-ASCII name, and
    `"tests/tier1/…"` does not start with `tests/`. Every prefix test then
    returns False and the diff is waved through, so this is the one input
    that must not be guessed at."""
    with pytest.raises(ValueError, match="quoted path"):
        guard.read_paths('src/mod.py\n"tests/tier1/test_\\303\\274nicode.py"\n')


def test_paths_arrive_nul_separated_from_the_workflow(guard: Any) -> None:
    """`-z` is what stops the quoting above, so NUL is the real format and
    newlines are the by-hand one."""
    assert guard.read_paths(f"{_SRC}\0{_TIER1}\0") == [_SRC, _TIER1]
    assert guard.read_paths(f"{_SRC}\n{_TIER1}\n") == [_SRC, _TIER1]


def test_the_guards_own_parts_are_gated_alongside_src(guard: Any) -> None:
    """The decider and the workflow are the guard's moving parts, and
    editing either beside `src/` is the same signature as editing an
    assertion beside `src/`. It used to be the way through."""
    assert guard.verdict([_SRC, "scripts/tier1_guard.py"], []) is not None
    assert guard.verdict([_SRC, ".github/workflows/tier1-guard.yml"], []) is not None
