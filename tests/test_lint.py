"""The instrumentation linter, pinned against the demo and against silence.

Two kinds of test here, and the second kind is the load-bearing one.

**Each rule fires on the shape it names.** Written against synthetic modules
rather than line numbers in `examples/demo.py`, because the demo is edited
often and a test asserting `line 145` fails for reasons that have nothing to
do with this module. The demo is still asserted, but on *which* rule fires in
*which* function.

**Each rule stays quiet on a well-logged file.** `test_idiomatic_logging_*`
is the test that makes every other test in this file mean something: without
it, a rule that fires on absolutely everything passes all the positive cases.
The gate the linter puts on its silent-loop findings is the whole reason it is
usable on a real codebase, so it is asserted directly too.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess  # nosec B404 - these tests launch real child processes
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from lumberjack import lint, static

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "examples" / "demo.py"


@pytest.fixture
def rules(write_module: Callable[..., Path]) -> Callable[..., list[str]]:
    """The rule slugs a snippet produces, in the order they are reported."""

    def _rules(source: str, *, all_loops: bool = False) -> list[str]:
        path = write_module(source)
        report = lint.check([str(path)], all_loops=all_loops)
        if report.files != 1:
            raise AssertionError("the fixture source should parse")
        return [finding.rule for finding in report.findings]

    return _rules


@pytest.fixture
def one(write_module: Callable[..., Path]) -> Callable[..., lint.Finding]:
    """The single finding a snippet produces, asserting there is exactly one."""

    def _one(source: str, *, all_loops: bool = False) -> lint.Finding:
        path = write_module(source)
        report = lint.check([str(path)], all_loops=all_loops)
        if report.files != 1:
            raise AssertionError("the fixture source should parse")
        findings = report.findings
        if len(findings) != 1:
            raise AssertionError(f"expected one finding, got {findings}")
        return findings[0]

    return _one


@pytest.fixture
def demo_report() -> lint.Report:
    return lint.check([str(DEMO)])


# --------------------------------------------------------------------------
# The one that makes the rest mean something
# --------------------------------------------------------------------------

IDIOMATIC = """
import logging

log = logging.getLogger(__name__)


def extract(count):
    for i in range(count):
        log.debug("fetched row %d from source table", i)


def reconcile(batches, rows):
    for batch in range(batches):
        log.debug("reconciling batch %d", batch)
        for row in range(rows):
            log.debug("compared row %d against ledger", row)


def fan_out(workers):
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def render(rows):
    lines = []
    for row in rows:
        lines.append(str(row))
    return "\\n".join(lines)
"""


def test_idiomatic_logging_produces_no_findings(
    rules: Callable[..., list[str]],
) -> None:
    """A well-logged file is silent — including its plumbing loops.

    `fan_out` and `render` are the shapes that make a naive
    "report every loop with no log line" rule unusable: they are loops, they
    log nothing, and telling anyone to narrate `worker.start()` is advice
    that costs the linter its credibility. Neither function logs at all, so
    nothing in the source says they are work worth watching.
    """
    assert rules(IDIOMATIC) == []


def test_the_silent_loop_gate_is_what_holds_those_back(
    rules: Callable[..., list[str]],
) -> None:
    # The findings are real and reachable; they are held back by a judgement
    # about noise, not by an inability to see them. `--all-loops` says so.
    assert rules(IDIOMATIC, all_loops=True) == [
        "loop-not-logged",
        "loop-not-logged",
        "loop-not-logged",
    ]


def test_a_file_that_never_imports_logging_is_not_advised(
    rules: Callable[..., list[str]], write_module: Callable[..., Path]
) -> None:
    """#61, measured: `parser.error(...)` is not a log call, and looks like one.

    The walk is receiver-blind, so any `X.error(…)` is a candidate. Across
    this venv's installed packages that is 110 non-loggers in 566 matches,
    and it would have made half of the `fstring-log-call` findings wrong —
    `parser.error(f"…")` in argparse code, told to use lazy %-formatting.

    Requiring the file to import `logging` is the cheap corroboration. It
    costs about 6% of real findings and removes 98% of the false ones, which
    is the direction a linter has to err in.
    """
    argparse_shaped = """
        import argparse
        {extra}
        def main():
            parser = argparse.ArgumentParser()
            args = parser.parse_args()
            if not args.path:
                parser.error(f"no such file: {{args.path}}")
            for item in args.items:
                process(item)
    """
    assert rules(argparse_shaped.format(extra="")) == []

    # The identical file, with logging in it, is advised — so the gate is
    # what is holding it back and not some other silence in the rules.
    #
    # And this is the residual the gate cannot remove: the 2-in-110 case
    # where a non-logger call lives in a file that does import `logging`.
    # `parser.error` is counted as narration, so the loop below is reported
    # as logging around itself. Pinned rather than hidden — the advice
    # ("log inside that loop") happens to be right and the premise is not.
    with_logging = argparse_shaped.format(extra="import logging")
    report = lint.check([str(write_module(with_logging))])
    assert [finding.rule for finding in report.findings] == [
        "loop-logs-around",
        "fstring-log-call",
    ]


def test_the_summary_admits_it_skipped_those_files(
    write_module: Callable[..., Path],
) -> None:
    # "The linter said nothing" and "the linter did not look" must be
    # tellable apart, or a quiet run means nothing.
    write_module("def f(x):\n    parser.error('bad')\n")
    report = lint.check([str(write_module(IDIOMATIC).parent)])

    assert report.files_without_logging == 1
    assert "never import `logging`" in lint.format_report(report)


# --------------------------------------------------------------------------
# One rule at a time
# --------------------------------------------------------------------------


def test_a_loop_logging_only_before_and_after(one: Callable[..., lint.Finding]) -> None:
    finding = one("""
        import logging

        def run(items):
            log.info("processing %d items", len(items))
            for item in items:
                transform(item)
                store(item)
            log.info("done")
        """)

    assert finding.rule == "loop-logs-around"
    assert finding.lineno == 5
    assert "not inside" in finding.what
    # The fix is the log line. `track()` is allowed only in `also`.
    assert "log.debug" in finding.fix
    assert finding.also is not None and "track(" in finding.also


def test_an_outer_loop_that_says_nothing_while_its_inner_one_logs(
    one: Callable[..., lint.Finding],
) -> None:
    finding = one("""
        import logging

        def run(batches):
            for batch in batches:
                for row in batch:
                    log.debug("compared row %s", row)
        """)

    assert finding.rule == "loop-not-logged"
    assert finding.lineno == 4
    # The inner loop's *total* comes from the ratio to an enclosing period, so
    # the missing outer line costs more than one row.
    assert "ratio" in finding.what


def test_a_dark_loop_beside_a_narrated_one(one: Callable[..., lint.Finding]) -> None:
    finding = one("""
        import logging

        def run(a, b):
            for x in a:
                log.debug("saw %s", x)
            for y in b:
                compute(y)
        """)

    assert finding.rule == "loop-not-logged"
    assert finding.lineno == 6
    assert "another loop (line 5)" in finding.what


def test_a_loop_written_on_one_line_reads_as_one_line(
    one: Callable[..., lint.Finding],
) -> None:
    finding = one("""
        import logging

        def run(a, b):
            for x in a:
                log.debug("saw %s", x)
            for y in b: compute(y)
        """)

    # "lines 4-4" would be a small lie about a loop whose body is inline.
    assert "at line 6 in run()" in finding.what
    assert "lines 6" not in finding.what


def test_a_module_level_loop_is_not_described_as_being_in_a_function(
    one: Callable[..., lint.Finding],
) -> None:
    finding = one("""
        import logging

        log.info("starting")
        for item in items:
            compute(item)
        """)

    assert finding.rule == "loop-logs-around"
    # `funcName` reads `<module>` here. That is a true fact about the record
    # and a strange thing to say to a person, so prose gets its own noun.
    assert "<module>" not in finding.what
    assert finding.what.startswith(
        "this module logs at line 3 but not inside the `for` loop at lines 4-5."
    )


def test_a_slow_body_narrated_by_one_line(one: Callable[..., lint.Finding]) -> None:
    finding = one("""
        import logging

        def run(batches):
            for batch in batches:
                log.debug("batch %d", batch)
                open_connection(batch)
                fetch_manifest(batch)
                validate_checksums(batch)
                write_output(batch)
                commit(batch)
        """)

    assert finding.rule == "one-line-slow-body"
    assert "5 statements" in finding.what


def test_a_body_that_does_little_is_not_a_slow_body(
    rules: Callable[..., list[str]],
) -> None:
    # `extract` in the demo: one log line and a sleep. One line is plenty, and
    # a linter that asks for more here is asking for noise.
    assert rules("""
        import logging

        def run(count):
            for i in range(count):
                log.debug("fetched row %d", i)
                time.sleep(0.1)
        """) == []


def test_stages_in_sequence_with_nothing_announcing_them(
    one: Callable[..., lint.Finding],
) -> None:
    finding = one("""
        import logging

        def run(files, records, rows):
            for f in files:
                log.debug("found input file %s", f)
            for r in records:
                log.debug("parsed record %s", r)
            for row in rows:
                log.debug("joined row %s", row)
        """)

    assert finding.rule == "no-stage-announcements"
    assert "3 loops in sequence" in finding.what
    assert "lines 4, 6, 8" in finding.what
    assert "log.info" in finding.fix
    # Rung 3 is offered second and hedged, never as the answer.
    assert finding.also is not None and "task(" in finding.also


def test_one_announcement_line_settles_it(rules: Callable[..., list[str]]) -> None:
    assert rules("""
        import logging

        def run(files, records, rows):
            log.info("stage 1: discovering input files")
            for f in files:
                log.debug("found input file %s", f)
            log.info("stage 2: parsing records")
            for r in records:
                log.debug("parsed record %s", r)
            log.info("stage 3: joining")
            for row in rows:
                log.debug("joined row %s", row)
        """) == []


def test_a_logging_wrapper_without_stacklevel(one: Callable[..., lint.Finding]) -> None:
    finding = one("""
        import logging

        def log_helper(message, *args):
            log.debug(message, *args)
        """)

    assert finding.rule == "wrapper-no-stacklevel"
    assert finding.lineno == 4
    assert "stacklevel=2" in finding.fix


def test_a_wrapper_that_passes_stacklevel_is_fine(
    rules: Callable[..., list[str]],
) -> None:
    assert rules("""
        import logging

        def log_helper(message, *args):
            log.debug(message, *args, stacklevel=2)
        """) == []


def test_a_wrapper_forwarding_kwargs_is_not_accused(
    rules: Callable[..., list[str]],
) -> None:
    # `**kwargs` may carry a stacklevel this walk cannot see, and accusing a
    # call that already does the right thing is the expensive kind of wrong.
    assert rules("""
        import logging

        def log_helper(message, *args, **kwargs):
            log.debug(message, *args, **kwargs)
        """) == []


def test_a_local_variable_is_not_a_wrapper(rules: Callable[..., list[str]]) -> None:
    # Only a *parameter* is the caller's message. A local is this function's
    # own text, and this function is where it should be attributed.
    assert rules("""
        import logging

        def run():
            message = build_message()
            log.debug(message)
        """) == []


def test_an_fstring_destroys_the_template(one: Callable[..., lint.Finding]) -> None:
    finding = one("""
        import logging

        def run(i):
            log.debug(f"row {i} parsed")
        """)

    assert finding.rule == "fstring-log-call"
    assert "template" in finding.what
    assert "%d" in finding.fix
    assert finding.also is not None and "G001-G004" in finding.also


def test_nothing_repeating_at_all(one: Callable[..., lint.Finding]) -> None:
    finding = one("""
        import logging

        def start_up():
            log.info("reading configuration")
            log.info("connecting to warehouse")
            log.info("warming schema cache")
            log.info("ready")
        """)

    assert finding.rule == "nothing-repeating"
    assert "4 times" in finding.what
    # A one-shot routine is not a defect, so it must not fail anybody's build.
    assert not finding.gates


def test_two_startup_lines_are_not_worth_a_paragraph(
    rules: Callable[..., list[str]],
) -> None:
    assert rules("""
        import logging

        def render():
            log.info("rendering 2.4M points at dpi=200")
            log.info("wrote figure.png")
        """) == []


# --------------------------------------------------------------------------
# The ordering rule
# --------------------------------------------------------------------------


def test_no_fix_ever_leads_with_the_expensive_advice(
    write_module: Callable[..., Path],
) -> None:
    """Idiomatic logging first, `track()` second — asserted as a rule.

    Where the display cannot infer something, the first question is whether
    an ordinary log line would have supplied it. `fix` is that answer and
    must never be the instrumentation one; `also` is where `track()` and
    `task()` are allowed to appear, beneath it.
    """
    quiet = write_module(IDIOMATIC, name="a.py")
    loud = write_module(ALL_SHAPES, name="b.py")
    findings = [
        *lint.check([str(quiet)], all_loops=True).findings,
        *lint.check([str(loud)]).findings,
    ]
    assert findings

    for finding in findings:
        assert "track(" not in finding.fix, finding.rule
        assert "task(" not in finding.fix, finding.rule
        assert "lumberjack" not in finding.fix, finding.rule


def test_findings_are_reported_in_order_of_value(
    write_module: Callable[..., Path],
) -> None:
    findings = lint.check([str(write_module(ALL_SHAPES))]).findings

    ranks = [lint.RULE_ORDER.index(finding.rule) for finding in findings]
    assert ranks == sorted(ranks)
    # #40's table, top to bottom, and every one of it reachable.
    assert set(finding.rule for finding in findings) == set(lint.RULE_ORDER) - {
        "nothing-repeating"
    }


def test_every_finding_names_a_file_a_line_and_a_change(
    write_module: Callable[..., Path],
) -> None:
    path = write_module(ALL_SHAPES)
    findings = lint.check([str(path)]).findings
    assert findings

    for finding in findings:
        assert finding.pathname == str(path)
        assert finding.lineno >= 1
        # "add a log line inside this loop" beats "this loop is not
        # instrumented", so a fix has to contain something to type.
        assert "`" in finding.fix, finding.rule
        rendered = lint.format_finding(finding)
        assert rendered.startswith(f"{path}:{finding.lineno}: {finding.rule}")


ALL_SHAPES = """
import logging

log = logging.getLogger(__name__)


def logs_around(items):
    log.info("processing %d items", len(items))
    for item in items:
        transform(item)


def outer_is_dark(batches):
    for batch in batches:
        for row in batch:
            log.debug("compared row %s", row)


def slow_body(batches):
    for batch in batches:
        log.debug("batch %d", batch)
        open_connection(batch)
        fetch_manifest(batch)
        validate_checksums(batch)
        write_output(batch)
        commit(batch)


def stages(files, records, rows):
    for f in files:
        log.debug("found input file %s", f)
    for r in records:
        log.debug("parsed record %s", r)
    for row in rows:
        log.debug("joined row %s", row)


def wrapper(message, *args):
    log.debug(message, *args)


def fstring(i):
    log.debug(f"row {i} parsed")
"""


# --------------------------------------------------------------------------
# The demo, which is the fixture set
# --------------------------------------------------------------------------


def _rules_at(report: lint.Report, func_name: str) -> set[str]:
    """Rules reported inside `func_name`, resolved through the demo's AST."""
    structure = static.analyze_file(str(DEMO))
    if structure is None:
        raise AssertionError(f"{DEMO} should be readable and parseable")
    lines = {
        loop.lineno for loop in structure.loops.values() if loop.func_name == func_name
    } | {
        site.lineno
        for site in structure.call_sites.values()
        if site.func_name == func_name
    }
    return {finding.rule for finding in report.findings if finding.lineno in lines}


def test_the_wrapped_scenario_reports_the_missing_stacklevel(
    demo_report: lint.Report,
) -> None:
    # `python examples/demo.py wrapped` collapses 400 records from three
    # threads onto one bar. #37 says the fix is one keyword in the user's
    # wrapper and inference should not chase it — so this is the only place
    # that failure is ever diagnosed.
    assert _rules_at(demo_report, "_log_via_wrapper") == {"wrapper-no-stacklevel"}


def test_the_oneshot_scenario_reports_that_nothing_repeats(
    demo_report: lint.Report,
) -> None:
    # #40: say outright that there is little to work with, rather than letting
    # an empty display imply a bug.
    assert _rules_at(demo_report, "run_oneshot") == {"nothing-repeating"}


def test_the_demo_is_otherwise_quiet(demo_report: lint.Report) -> None:
    """Every other scenario is well-logged, and that is the demo's whole point.

    Worth pinning rather than assuming. The scenarios are shapes of *display*
    problems — what should a person see here — and only `wrapped` and
    `oneshot` are also shapes of *logging* problems. If a scenario is ever
    edited into an under-logged one, this catches it.
    """
    assert len(demo_report.findings) == 2
    # Exactly one of the two is a defect. `wrapped` carries a genuinely broken
    # wrapper on purpose, so the demo *should* fail a gate; `oneshot` is a
    # correct one-shot routine and must not.
    assert [finding.rule for finding in demo_report.gating] == ["wrapper-no-stacklevel"]


def test_the_demos_bare_loops_are_held_back_but_reachable() -> None:
    """`run_pipeline`'s two loops, which are the reason for the gate.

    `for worker in workers: worker.start()` is a loop with no log line in its
    body, which #40 calls the highest-value finding. It is also thread fan-out
    that finishes instantly, and `run_pipeline` logs nothing anywhere, so
    nothing in the source separates it from real work. Reported on request,
    never by default, and never gating.
    """
    default = lint.check([str(DEMO)])
    assert not any(finding.rule == "loop-not-logged" for finding in default.findings)

    verbose = lint.check([str(DEMO)], all_loops=True)
    pipeline = _lines_for(verbose, "run_pipeline")
    assert len(pipeline) == 2
    assert all(not finding.gates for finding in verbose.findings if finding in pipeline)


def _lines_for(report: lint.Report, func_name: str) -> list[lint.Finding]:
    structure = static.analyze_file(str(DEMO))
    if structure is None:
        raise AssertionError(f"{DEMO} should be readable and parseable")
    lines = {
        loop.lineno for loop in structure.loops.values() if loop.func_name == func_name
    }
    return [finding for finding in report.findings if finding.lineno in lines]


def test_the_summary_says_what_it_held_back(demo_report: lint.Report) -> None:
    rendered = lint.format_report(demo_report)

    # A linter that silently drops findings is indistinguishable from one that
    # cannot see them, so the count and the flag that reveals them are stated.
    assert "27 loops" in rendered
    assert "14 more loop(s) have no log line" in rendered
    assert "--all-loops" in rendered


# --------------------------------------------------------------------------
# #58 — the agent rules, and the drift guard on them
# --------------------------------------------------------------------------


def _demo_rule_leads() -> list[str]:
    """The bold lead of each bullet in the demo's "How to log" section."""
    tree = ast.parse(DEMO.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree)
    if docstring is None:
        raise AssertionError("examples/demo.py should have a module docstring")
    header = "## How to log so this works"
    if header not in docstring:
        raise AssertionError(f"{header!r} is where the canonical wording lives")
    section = docstring.split(header, 1)[1]
    bullets = re.findall(r"^- \*\*(.+?)\*\*", section, re.MULTILINE | re.DOTALL)
    return [" ".join(bullet.split()) for bullet in bullets]


def test_the_agent_rules_quote_the_demo_verbatim() -> None:
    """#58's sync guard: the two wordings cannot drift apart unnoticed.

    `examples/demo.py` owns the canonical text and this module cannot edit
    it, so the only thing keeping them together is this assertion. It pins
    the bold lead of each bullet, in order — reword, reorder, add or remove
    a rule there and this fails, which is the point.
    """
    leads = _demo_rule_leads()
    assert len(leads) == 5, f"the demo's list changed shape: {leads}"

    block = lint.format_agent_rules()
    normalised = " ".join(block.split())
    for lead in leads:
        assert f"**{lead}**" in normalised, lead

    # In order, so a reordering is caught as well as a rewording.
    positions = [normalised.index(f"**{lead}**") for lead in leads]
    assert positions == sorted(positions)


def test_the_agent_rules_add_the_one_the_demo_does_not_carry() -> None:
    # #58 lists lazy %-formatting; the demo's five bullets do not. Asserted so
    # the extra is deliberate rather than something that drifted in — see the
    # comment above AGENT_RULES and issue #65.
    normalised = " ".join(lint.AGENT_RULES.split())
    assert "never an f-string" in normalised
    assert "G001-G004" in normalised
    assert not any("f-string" in lead for lead in _demo_rule_leads())


def test_the_agent_rules_say_not_to_delete_the_logging() -> None:
    # The entire reason #58 exists: "remove the temporary debug logging" is
    # near-universal agent tidying, and here it deletes the display.
    block = lint.AGENT_RULES
    assert "Do not remove debug logging while cleaning up" in block
    assert "python -m lumberjack.lint" in block


def test_the_agent_rules_name_this_codebases_own_gaps(
    demo_report: lint.Report,
) -> None:
    # A generic rule list is easy to ignore; a specific one is not. That
    # specificity is #58's whole argument for the linter owning this.
    #
    # The line number comes from the report rather than being written in
    # here. `examples/demo.py` is edited often — a scenario gains a comment
    # and every line below it moves — and a hardcoded `demo.py:310` then
    # fails for a reason that has nothing to do with the linter. That has
    # already happened once, and `tests/test_static.py` opens by warning
    # about exactly this.
    block = lint.format_agent_rules(demo_report)
    wrapper = next(
        finding
        for finding in demo_report.findings
        if finding.rule == "wrapper-no-stacklevel"
    )

    assert "### What this codebase is missing right now" in block
    assert f"`{DEMO}:{wrapper.lineno}` — wrapper-no-stacklevel" in block
    # And the line really is the wrapper, not merely whatever the report said.
    source = DEMO.read_text(encoding="utf-8").splitlines()
    assert "log.debug(" in source[wrapper.lineno - 1]


def test_a_clean_codebase_gets_the_block_and_no_scolding(
    write_module: Callable[..., Path],
) -> None:
    clean = lint.check([str(write_module(IDIOMATIC))])
    assert clean.findings == ()

    block = lint.format_agent_rules(clean)
    assert block == lint.AGENT_RULES
    assert "missing right now" not in block


def test_the_appended_list_is_bounded(write_module: Callable[..., Path]) -> None:
    many = "import logging\n\n" + "\n\n".join(
        f"def wrapper{i}(message):\n    log.debug(message)" for i in range(12)
    )
    report = lint.check([str(write_module(many))])
    assert len(report.findings) == 12

    block = lint.format_agent_rules(report)
    assert block.count("— wrapper-no-stacklevel") == lint.AGENT_RULES_EXAMPLES
    assert "… and 4 more." in block


# --------------------------------------------------------------------------
# Walking, and the CLI
# --------------------------------------------------------------------------


def test_a_directory_walk_skips_what_is_not_your_code(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "notes.txt").write_text("hello\n", encoding="utf-8")
    for skipped in (".venv", "__pycache__", "build", "node_modules"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "junk.py").write_text("x = 1\n", encoding="utf-8")

    found = lint.python_files([str(tmp_path)])

    assert found == [str(tmp_path / "pkg" / "app.py")]


def test_a_named_file_is_taken_as_given(tmp_path: Path) -> None:
    # Someone pointing at one file means that file, suffix or no suffix.
    odd = tmp_path / "script"
    odd.write_text("x = 1\n", encoding="utf-8")

    assert lint.python_files([str(odd)]) == [str(odd)]


def test_an_unreadable_file_is_skipped_rather_than_crashing(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")

    report = lint.check([str(tmp_path)])
    assert report.files == 0
    assert report.findings == ()


def test_the_cli_gates_on_findings(
    write_module: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_module(ALL_SHAPES)

    assert lint.main([str(path)]) == lint.EXIT_FINDINGS

    out = capsys.readouterr().out
    assert "wrapper-no-stacklevel" in out
    assert f"{path}:" in out


def test_the_cli_is_silent_and_clean_on_idiomatic_code(
    write_module: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_module(IDIOMATIC)

    assert lint.main([str(path)]) == 0
    assert "No findings." in capsys.readouterr().out


def test_a_non_gating_finding_alone_does_not_fail(
    write_module: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    # A one-shot script is allowed to be one-shot. It is still reported.
    path = write_module("""
        import logging

        def start_up():
            log.info("reading configuration")
            log.info("connecting")
            log.info("ready")
        """)

    assert lint.main([str(path)]) == 0
    assert "nothing-repeating" in capsys.readouterr().out


def test_nothing_to_analyse_is_its_own_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A mistyped path must not look like a clean run, or a gate passes when it
    # should not.
    assert lint.main([str(tmp_path / "absent.py")]) == lint.EXIT_NO_FILES
    assert "Nothing to analyse." in capsys.readouterr().err


def test_the_default_path_is_the_current_directory(
    tmp_path: Path,
    write_module: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_module(ALL_SHAPES)
    monkeypatch.chdir(tmp_path)

    assert lint.main([]) == lint.EXIT_FINDINGS
    assert "wrapper-no-stacklevel" in capsys.readouterr().out


def test_agent_rules_prints_and_never_gates(
    write_module: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_module(ALL_SHAPES)

    # It is meant to be piped into a file: `>> CLAUDE.md` under `set -e` must
    # not fail because the findings it is describing exist.
    assert lint.main([str(path), "--agent-rules"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("<!-- lumberjack:")
    assert "What this codebase is missing right now" in out


def test_all_loops_reports_more_and_still_exits_clean(
    write_module: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_module(IDIOMATIC)

    assert lint.main([str(path), "--all-loops"]) == 0

    out = capsys.readouterr().out
    assert out.count("loop-not-logged") == 3


def test_the_module_runs_as_a_command() -> None:
    """`python -m lumberjack.lint`, which is how it is documented.

    A subprocess because that entry point is the whole user-facing surface
    and nothing else exercises it — an import-level test would pass with the
    `__main__` guard deleted.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), env.get("PYTHONPATH", "")])
    env["COVERAGE_PROCESS_START"] = str(REPO / "pyproject.toml")

    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "lumberjack.lint", str(DEMO)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == lint.EXIT_FINDINGS, result.stderr
    assert "wrapper-no-stacklevel" in result.stdout
