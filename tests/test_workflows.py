"""A workflow that will not parse runs nothing, and says so quietly.

This exists because of a specific failure: a job was added with the id
`slow tests`. GitHub requires a job id to match `[A-Za-z_][A-Za-z0-9_-]*`,
so the space made the whole of `ci.yml` invalid — and the result was not a
red check but **no checks at all**. The pull request showed six green ones
from other workflows, the required ones simply never reported, and it looked
mergeable.

That is the worst failure shape available: it removes the signal instead of
raising one, and every downstream check that would have caught it is exactly
the thing that stopped running. So the schema is asserted here, where it runs
before the push rather than after — the same reasoning as `test_toolchain.py`.

These check the parts a YAML parser will not: it accepts `slow tests:` as a
mapping key perfectly happily.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

#: https://docs.github.com/actions — job ids "must start with a letter or _
#: and contain only alphanumeric characters, - or _".
_JOB_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def _workflows() -> list[Path]:
    return sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml"))


def _jobs(path: Path) -> dict[str, dict]:
    """The workflow's `jobs` mapping, or empty if it has none.

    `yaml.safe_load` returns None for a file that is empty or all comments,
    and `None.get` is an AttributeError — so a placeholder workflow would have
    made these tests *error* rather than report. That matters more here than
    it usually would: this file exists because an unparseable workflow fails
    by going silent, and a guard that crashes on the edge case is a guard that
    has to be debugged at the moment it is most needed.
    """
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded.get("jobs", {}) or {}


def test_there_are_workflows_to_check() -> None:
    """A parametrized rule over an empty list passes for the wrong reason."""
    assert _workflows()


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_every_job_id_is_one_github_accepts(path: Path) -> None:
    jobs = _jobs(path)
    bad = [name for name in jobs if not _JOB_ID.fullmatch(name)]
    assert bad == [], f"{path.name} has job ids GitHub will reject: {bad}"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_every_needs_names_a_job_that_exists(path: Path) -> None:
    """A `needs:` pointing at nothing is the same class of defect — the
    workflow is rejected whole, and nothing runs."""
    jobs = _jobs(path)
    for name, body in jobs.items():
        needs = body.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        missing = [dep for dep in needs if dep not in jobs]
        assert missing == [], f"{path.name}: job {name!r} needs {missing}, absent"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_the_dependency_graph_is_acyclic(path: Path) -> None:
    """A cycle is accepted by the parser and rejected by GitHub."""
    jobs = _jobs(path)
    graph = {}
    for name, body in jobs.items():
        needs = body.get("needs") or []
        graph[name] = [needs] if isinstance(needs, str) else list(needs)
    resolved: set[str] = set()
    progress = True
    while progress:
        progress = False
        for name, deps in graph.items():
            if name not in resolved and all(d in resolved for d in deps):
                resolved.add(name)
                progress = True
    assert set(graph) == resolved, f"{path.name}: cycle among {set(graph) - resolved}"


def test_a_workflow_with_no_jobs_is_handled_rather_than_crashing(tmp_path) -> None:
    """The edge case the helper exists for, driven directly: an empty file, a
    comment-only file, and one with a `jobs:` key and nothing under it."""
    for text in ("", "# nothing here\n", "name: x\njobs:\n"):
        path = tmp_path / "w.yml"
        path.write_text(text, encoding="utf-8")
        assert _jobs(path) == {}, repr(text)


def test_the_only_pty_coverage_in_the_repository_cannot_be_deleted_quietly() -> None:
    """Detection → rich is asserted nowhere in the suite.

    Every tier-1 script either forces `output_mode` or, in
    `piped_is_plain.py`'s case, proves detection picks *plain*. The rich
    half needs a real terminal, so the only thing in the repository that
    exercises it is `demo-gif.yml`, which drives `examples/demo.py` under a
    pty and fails unless a frame actually drew a bar.

    That signal rests on three separate things, none of which the suite
    noticed: the workflow existing, `--require '━'` still being passed, and
    `demo.py` still leaving `output_mode` unset so detection is what
    chooses. Remove any one and the recording silently becomes a film of
    the plain renderer — which has happened once already, when `rich` was
    missing from the recorder's environment and a fallback gif shipped.

    Not a substitute for closing the gap; a tripwire on the workaround.
    `demo-gif.yml` is not a required check, so this cannot make it one.
    """
    demo_gif = _WORKFLOWS / "demo-gif.yml"
    assert demo_gif.exists(), "the only pty coverage in the repository is gone"
    assert "--require '━'" in demo_gif.read_text(encoding="utf-8")

    demo = _WORKFLOWS.parent.parent / "examples" / "demo.py"
    source = demo.read_text(encoding="utf-8")
    assert "lumberjack.init()" in source, (
        "demo.py must leave output_mode unset, or the recording stops "
        "exercising detection and only exercises an override"
    )
