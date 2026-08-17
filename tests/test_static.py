"""Static structure extraction, pinned against the demo and against runtime.

`examples/demo.py` is the primary fixture: it carries the loop shapes the
display is designed around, so the assertions here are the same shapes the
issues argue about. They are written against *templates* rather than line
numbers on purpose — the demo is edited often, and a test that hardcodes
`line 164` fails for a reason that has nothing to do with this module.

The load-bearing test is `test_ast_linenos_match_the_records_they_emit`: it
compiles a file of awkward call shapes, runs it, and checks that every
`LogRecord.lineno` finds the call site the walk recorded. That is the one
assumption the whole module rests on, it is the one CPython could change
between versions, and CI runs it on 3.12, 3.13 and 3.14.
"""

from __future__ import annotations

import itertools
import logging
import os
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import pytest

from lumberjack import static

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo.py"


@pytest.fixture(autouse=True)
def _clear_static_cache() -> Iterator[None]:
    # Every test writes its own file, but the cache is module state and a test
    # that edits a path another test parsed would otherwise read a stale entry.
    static.clear_cache()
    yield
    static.clear_cache()


@pytest.fixture
def write_module(tmp_path: Path) -> Callable[..., Path]:
    """Write dedented source to a uniquely named module and return its path."""
    counter = itertools.count()

    def _write(source: str, *, name: str | None = None) -> Path:
        path = tmp_path / (name or f"mod{next(counter)}.py")
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def analyze(write_module: Callable[..., Path]) -> Callable[..., static.FileStructure]:
    def _analyze(source: str, *, name: str | None = None) -> static.FileStructure:
        structure = static.analyze_file(str(write_module(source, name=name)))
        assert structure is not None, "the fixture source should parse"
        return structure

    return _analyze


@pytest.fixture
def demo() -> static.FileStructure:
    structure = static.analyze_file(str(DEMO))
    assert structure is not None, f"{DEMO} should be readable and parseable"
    return structure


def loop_in(structure: static.FileStructure, func_name: str) -> static.Loop:
    """The one loop in `func_name` that has call sites."""
    loops = [
        loop
        for loop in structure.loops.values()
        if loop.func_name == func_name and loop.call_sites
    ]
    assert len(loops) == 1, f"expected one logging loop in {func_name}, got {loops}"
    return loops[0]


def templates(loop: static.Loop) -> list[str | None]:
    return [site.template for site in loop.call_sites]


# --------------------------------------------------------------------------
# The demo's known shapes
# --------------------------------------------------------------------------


def test_sequence_is_one_body_of_five_in_textual_order(
    demo: static.FileStructure,
) -> None:
    loop = loop_in(demo, "run_sequence")

    assert loop.depth == 1
    assert loop.parent is None
    assert loop.kind == "for"
    assert templates(loop) == [
        "batch %d: opening connection",
        "batch %d: fetching manifest",
        "batch %d: validating checksums",
        "batch %d: writing output",
        "batch %d: committing",
    ]
    # #53's whole ask: position within the body, known before anything runs.
    assert [site.position for site in loop.call_sites] == [1, 2, 3, 4, 5]
    assert {site.body_size for site in loop.call_sites} == {5}
    assert loop.stable_order
    # Textual order is source order, and the sites are the five in the body.
    assert [site.lineno for site in loop.call_sites] == sorted(
        site.lineno for site in loop.call_sites
    )


def test_siblings_are_four_call_sites_in_one_body(demo: static.FileStructure) -> None:
    loop = loop_in(demo, "run_siblings")

    # #8: four identities, one loop. The display unit is this loop; source
    # location stays the identity underneath it.
    assert templates(loop) == [
        "row %d: parsed",
        "row %d: schema validated",
        "row %d: enriched from cache",
        "row %d: emitted downstream",
    ]
    assert all(site.depth == 1 for site in loop.call_sites)
    assert all(site.loop_lineno == loop.lineno for site in loop.call_sites)


def test_reconcile_is_two_loops_nested_one_inside_the_other(
    demo: static.FileStructure,
) -> None:
    loops = [loop for loop in demo.loops.values() if loop.func_name == "reconcile"]
    outer, inner = sorted(loops, key=lambda loop: loop.depth)

    assert (outer.depth, inner.depth) == (1, 2)
    assert outer.parent is None
    assert inner.parent == outer.lineno
    assert templates(outer) == ["reconciling batch %d"]
    assert templates(inner) == ["compared row %d against ledger"]

    # The containment the runtime model spends a ratio and several polls
    # inferring, stated by the source and scoped to the inner site's chain.
    (inner_site,) = inner.call_sites
    assert inner_site.loop_chain == (outer.lineno, inner.lineno)
    # Position is relative to the *innermost* body, so both are 1 of 1 rather
    # than the outer body counting the nested loop's line as its second site.
    assert (inner_site.position, inner_site.body_size) == (1, 1)


def test_a_call_under_an_if_is_conditional(demo: static.FileStructure) -> None:
    loop = loop_in(demo, "transform")
    unconditional, warning = loop.call_sites

    assert unconditional.template == "normalized record %d"
    assert not unconditional.conditional
    assert warning.method == "warning"
    assert warning.conditional
    # One conditional site is enough to make the body's order a guess, which
    # is what #53 needs to know before drawing a determinate sub-iteration bar.
    assert not loop.stable_order


def test_a_wrapped_logger_call_site_has_no_template(
    demo: static.FileStructure,
) -> None:
    # `_log_via_wrapper` forwards a variable, so no template survives to the
    # AST — and none survives to `record.msg` either, which is why the drift
    # guard has nothing to compare and refuses. The wrapper's *callers* are
    # invisible for a different reason: `_log_via_wrapper(...)` is not an
    # attribute call. Both are the documented #37 limitation.
    (site,) = [
        site
        for site in demo.call_sites.values()
        if site.func_name == "_log_via_wrapper"
    ]
    assert site.template is None
    assert site.loop_chain == ()

    worker_loops = [loop for loop in demo.loops.values() if loop.func_name == "worker"]
    assert worker_loops and all(loop.call_sites == () for loop in worker_loops)


def test_a_loop_that_logs_nothing_is_still_reported(
    demo: static.FileStructure,
) -> None:
    # Not an oversight: a loop with no call sites is exactly what the linter
    # (#40) exists to point at, and the display needs to know the loop is
    # there to say it cannot see inside it.
    silent = [
        loop
        for loop in demo.loops.values()
        if loop.func_name == "run_pipeline" and not loop.call_sites
    ]
    assert len(silent) == 2


# --------------------------------------------------------------------------
# Agreement with the interpreter
# --------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


AWKWARD_SHAPES = """
def emit(x):
    log.debug("single line %d", x)
    log.debug(
        "arguments on their own lines %d",
        x,
    )
    log.debug("trailing argument %d",
              x)
    (
        log
        .debug("receiver on its own line %d", x)
    )
    log \\
        .debug("backslash continuation %d", x)
    log.log(
        logging.DEBUG,
        "level first %d",
        x,
    )
    for i in range(1):
        if x:
            log.warning(
                "nested, conditional and multi-line %d",
                i,
            )
"""


def test_ast_linenos_match_the_records_they_emit(
    write_module: Callable[..., Path],
) -> None:
    """Every emitted record finds its call site, for every awkward call shape.

    The walk keys a call site on the line the *attribute* ends on, not on
    `Call.lineno`, which is where the whole expression starts. The two differ
    whenever the receiver spans lines, and it is the attribute's line that
    stdlib's `findCaller` reports. Asserting it against real records rather
    than against a remembered rule is what makes this survive a CPython
    change: if 3.15 moves the line, this fails rather than the display
    quietly losing every multi-line call site.
    """
    path = write_module(AWKWARD_SHAPES, name="shapes.py")
    source = path.read_text(encoding="utf-8")

    logger = logging.getLogger("test_static.shapes")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    capture = _Capture()
    logger.addHandler(capture)
    try:
        namespace: dict[str, object] = {"log": logger, "logging": logging}
        exec(compile(source, str(path), "exec"), namespace)
        emit = namespace["emit"]
        assert callable(emit)
        emit(1)
    finally:
        logger.removeHandler(capture)

    structure = static.analyze_file(str(path))
    assert structure is not None
    assert len(capture.records) == 7

    for record in capture.records:
        assert record.pathname == str(path)
        site = structure.call_sites.get(record.lineno)
        assert site is not None, f"no call site at line {record.lineno}: {record.msg}"
        assert site.template == record.msg
        assert site.func_name == record.funcName


def test_the_level_argument_is_not_mistaken_for_the_template(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(x):
            log.log(logging.INFO, "level first %d", x)
        """)

    (site,) = structure.call_sites.values()
    assert site.method == "log"
    assert site.template == "level first %d"


# --------------------------------------------------------------------------
# The drift guard
# --------------------------------------------------------------------------


DRIFTING = """
import logging

log = logging.getLogger(__name__)


def f(x):
    log.debug("original template %d", x)
"""


def test_template_matches_an_unedited_file(write_module: Callable[..., Path]) -> None:
    path = write_module(DRIFTING)
    structure = static.analyze_file(str(path))
    assert structure is not None
    (lineno,) = structure.call_sites

    assert static.template_matches(str(path), lineno, "original template %d")


def test_template_matches_refuses_a_drifted_file(
    write_module: Callable[..., Path],
) -> None:
    path = write_module(DRIFTING)
    structure = static.analyze_file(str(path))
    assert structure is not None
    (lineno,) = structure.call_sites

    # A comment inserted above the call: the line still exists, still holds a
    # log call, and now says something else. `record.msg` from the running
    # process is the only witness that the file moved under it.
    path.write_text(DRIFTING.lstrip().replace("original", "edited"), encoding="utf-8")
    _bump_mtime(path)

    assert not static.template_matches(str(path), lineno, "original template %d")
    assert static.template_matches(str(path), lineno, "edited template %d")


def test_template_matches_refuses_what_it_cannot_identify(
    write_module: Callable[..., Path], tmp_path: Path
) -> None:
    path = write_module(DRIFTING)

    # A line with no call site on it, a file that is not there, and a call
    # site whose template never survived to runtime all mean the same thing:
    # use nothing.
    assert not static.template_matches(str(path), 1, "original template %d")
    assert not static.template_matches(
        str(tmp_path / "absent.py"), 7, "original template %d"
    )

    fstring = write_module("""
        def f(x):
            log.debug(f"rendered {x}")
        """)
    structure = static.analyze_file(str(fstring))
    assert structure is not None
    (lineno,) = structure.call_sites
    assert not static.template_matches(str(fstring), lineno, "rendered 1")


def test_an_fstring_call_site_is_recorded_without_a_template(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(x):
            for i in range(3):
                log.debug("first %d", i)
                log.debug(f"second {x}")
                log.debug("third %d", i)
        """)

    sites = list(structure.call_sites.values())
    assert [site.template for site in sites] == ["first %d", None, "third %d"]
    # The f-string site still holds its slot. Dropping it because it has no
    # template would renumber "third" as 2 of 2 and quietly break the ordering
    # of every sibling after it.
    assert [site.position for site in sites] == [1, 2, 3]
    assert {site.body_size for site in sites} == {3}


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


def test_a_missing_file_is_none(tmp_path: Path) -> None:
    assert static.analyze_file(str(tmp_path / "nope.py")) is None


def test_a_directory_is_none(tmp_path: Path) -> None:
    assert static.analyze_file(str(tmp_path)) is None


def test_a_syntax_error_is_none(write_module: Callable[..., Path]) -> None:
    path = write_module("def f(:\n    pass\n")
    assert static.analyze_file(str(path)) is None


def test_null_bytes_are_none(tmp_path: Path) -> None:
    path = tmp_path / "nulls.py"
    path.write_bytes(b"x = 1\x00\n")
    assert static.analyze_file(str(path)) is None


def test_an_encoding_declaration_is_honoured(tmp_path: Path) -> None:
    # Read as bytes so `ast.parse` decodes the file the way the import
    # machinery did. Reading text would guess UTF-8 and raise on this.
    path = tmp_path / "latin.py"
    path.write_bytes(
        b"# -*- coding: latin-1 -*-\n"
        b"def f(x):\n"
        b"    log.debug('caf\xe9 %d', x)\n"
    )

    structure = static.analyze_file(str(path))
    assert structure is not None
    (site,) = structure.call_sites.values()
    assert site.template == "café %d"


# --------------------------------------------------------------------------
# What the walk does and does not attribute
# --------------------------------------------------------------------------


def test_a_call_outside_any_loop_has_no_position(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        log.info("module level")


        def f():
            log.info("function level")
        """)

    module_site, function_site = structure.call_sites.values()
    assert module_site.func_name == static.MODULE_SCOPE
    assert function_site.func_name == "f"
    for site in (module_site, function_site):
        assert site.loop_chain == ()
        assert site.loop_lineno is None
        assert site.depth == 0
        assert (site.position, site.body_size) == (None, None)


def test_while_and_async_for_are_loops(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        async def f(source):
            while True:
                log.debug("spinning")
            async for item in source:
                log.debug("streamed %s", item)
        """)

    kinds = {loop.kind: templates(loop) for loop in structure.loops.values()}
    assert kinds == {"while": ["spinning"], "async for": ["streamed %s"]}
    assert all(loop.func_name == "f" for loop in structure.loops.values())


def test_only_the_loop_body_repeats(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(source):
            for item in source:
                log.debug("in the body")
            else:
                log.debug("in the else clause")
        """)

    body, otherwise = structure.call_sites.values()
    assert body.template == "in the body"
    assert body.depth == 1
    # `for … else` runs once after the loop, so a bar built on it would tick
    # once per *loop*, not once per iteration.
    assert otherwise.template == "in the else clause"
    assert otherwise.loop_chain == ()


def test_a_function_defined_in_a_loop_is_not_in_that_loop(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(items):
            for item in items:
                def later():
                    log.debug("called who knows when")

                callback = lambda: log.debug("also who knows when")
                register(later, callback)
        """)

    nested, inside_lambda = structure.call_sites.values()
    # A closure defined in a loop body does not run per iteration, so
    # attributing its calls to the loop would invent a tick rate.
    assert nested.func_name == "later"
    assert nested.loop_chain == ()
    assert inside_lambda.func_name == "<lambda>"
    assert inside_lambda.loop_chain == ()


def test_conditional_is_relative_to_the_innermost_body(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(items, flag):
            for item in items:
                if flag:
                    for part in item:
                        log.debug("inner %s", part)
                match flag:
                    case True:
                        log.debug("matched")
                try:
                    log.debug("attempted")
                except ValueError:
                    log.debug("failed")
        """)

    sites = {site.template: site for site in structure.call_sites.values()}
    # The nested loop is itself conditional, but its own body is not: every
    # iteration of *that* loop runs this line, which is what a sub-iteration
    # bar is asking about.
    assert not sites["inner %s"].conditional
    assert sites["inner %s"].depth == 2
    for template in ("matched", "attempted", "failed"):
        assert sites[template].conditional, template


def test_two_call_sites_on_one_line_are_both_dropped(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(items):
            for item in items:
                log.debug("first"); log.debug("second")
                log.debug("third")
        """)

    # Two records from one line are indistinguishable, so neither site can be
    # identified from a record and both are refused. The survivor is 1 of 1:
    # counting sites nothing can look up would only misreport the body.
    (site,) = structure.call_sites.values()
    assert site.template == "third"
    assert (site.position, site.body_size) == (1, 1)


def test_calls_the_walk_does_not_recognise(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        from logging import debug


        def f(x):
            debug("imported straight from logging")
            log.warn("a deprecated alias")
            log.debug()
            log.debug(msg="passed by keyword")
            log.trace("not a logging method")
            other.compute(x)
        """)

    # Silence rather than a guess: each of these either is not a log call or
    # cannot be read as one, and inventing a call site would put a phantom
    # slot in a body's ordering.
    assert structure.call_sites == {}


def test_a_receiver_blind_match_accepts_any_attribute_call(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f(parser, records):
            self._logger.info("through a private attribute")
            logging.warning("through the module")
            parser.error("not a log call at all")
        """)

    # The documented trade: no name heuristic separates a logger from an
    # argparse parser, and missing a real call site is worse than accepting a
    # false one, which only ever costs a slot in a body's ordering.
    assert [site.template for site in structure.call_sites.values()] == [
        "through a private attribute",
        "through the module",
        "not a log call at all",
    ]


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def _bump_mtime(path: Path, *, seconds: int = 10) -> None:
    """Move mtime forward explicitly, rather than trusting clock resolution."""
    stat = path.stat()
    os.utime(path, (stat.st_atime + seconds, stat.st_mtime + seconds))


def test_an_unchanged_file_is_parsed_once(write_module: Callable[..., Path]) -> None:
    path = write_module(DRIFTING)

    assert static.analyze_file(str(path)) is static.analyze_file(str(path))


def test_an_edited_file_is_reparsed(write_module: Callable[..., Path]) -> None:
    path = write_module(DRIFTING)
    first = static.analyze_file(str(path))

    # Same length, so mtime is the only thing that changed and the only thing
    # that can invalidate the entry.
    path.write_text(DRIFTING.lstrip().replace("original", "modified"), "utf-8")
    _bump_mtime(path)

    second = static.analyze_file(str(path))
    assert second is not None and second is not first
    assert [site.template for site in second.call_sites.values()] == [
        "modified template %d"
    ]


def test_clear_cache_forces_a_reparse(write_module: Callable[..., Path]) -> None:
    path = write_module(DRIFTING)
    first = static.analyze_file(str(path))

    static.clear_cache()

    assert static.analyze_file(str(path)) is not first


def test_an_unparseable_file_is_not_reparsed_on_every_call(
    write_module: Callable[..., Path],
) -> None:
    path = write_module("def f(:\n    pass\n")

    # Caching the failure matters more than caching a success: a broken file
    # would otherwise be read and rejected on every redraw.
    #
    # The file stays on disk and `_parse` is counted, because an earlier
    # version of this test deleted it between the two calls — which made the
    # second one return None through the missing-file guard whether or not
    # failures were cached at all. It passed with the cache disabled entirely,
    # which is to say it pinned nothing.
    calls = 0
    real_parse = static._parse

    def counting_parse(pathname: str) -> static.FileStructure | None:
        nonlocal calls
        calls += 1
        return real_parse(pathname)

    with mock.patch.object(static, "_parse", counting_parse):
        assert static.analyze_file(str(path)) is None
        assert static.analyze_file(str(path)) is None

    assert path.exists(), "the file must stay put, or the guard answers instead"
    assert calls == 1, f"the failure was re-parsed: {calls} calls"


def test_the_result_cannot_be_mutated_by_a_caller(
    analyze: Callable[..., static.FileStructure],
) -> None:
    structure = analyze("""
        def f():
            log.info("only")
        """)

    # It is shared between every caller for the life of the process.
    with pytest.raises(TypeError):
        structure.call_sites[99] = next(iter(structure.call_sites.values()))  # type: ignore[index]
    with pytest.raises(TypeError):
        structure.loops[99] = None  # type: ignore[index]


def test_a_call_in_a_while_condition_belongs_to_the_loop(
    analyze: Callable[..., static.FileStructure],
) -> None:
    """A `while` test re-runs every iteration, so a call there is in the loop.

    Getting this wrong is not the harmless direction. The call fires once per
    pass at runtime, so attributing it outside the loop makes the static answer
    *contradict* the records — and consumers use static as a veto over runtime
    inference, so a contradiction is worse than an absence.

    `For.iter` is the genuine other case and is asserted alongside: it is
    evaluated once, before the loop, so it stays outside.
    """
    structure = analyze("""
        def poll(log, source, n):
            i = 0
            while log.debug("checking %d", i) or i < n:
                i += 1
            for item in log.info("starting %s", source) or []:
                log.debug("item %s", item)
        """)
    by_template = {site.template: site for site in structure.call_sites.values()}

    assert by_template["checking %d"].loop_chain != (), "while-test repeats"
    assert by_template["item %s"].loop_chain != (), "a body always repeats"
    assert by_template["starting %s"].loop_chain == (), "for-iter runs once"
