"""The second row: where a slow loop is within its current iteration (#53).

No `rich` here either — deciding whether the row is earned, and what it points
at, is model work and runs on a bare install.

Every fixture writes a real module to `tmp_path` and logs records keyed on it,
because the whole feature rests on reading that file: the ordinal comes from
the AST's view of the body, not from anything counted at runtime. A test that
mocked the structure out would be testing nothing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lumberjack import static
from lumberjack.renderers.progress import (
    MIN_LEGIBLE_PERIOD,
    BarState,
    CyclePositionModel,
    LoopRowModel,
)
from lumberjack.schema import SourceKey


@pytest.fixture(autouse=True)
def _fresh_static_cache():
    static.clear_cache()
    yield
    static.clear_cache()


# `sequence` in miniature: three stages narrating one slow body. The loop is
# on line 7 and the call sites on 8, 9 and 10.
SEQUENCE = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        log.debug("batch %d: fetching manifest", batch)
        log.debug("batch %d: validating checksums", batch)
"""

# `transform` in miniature: the middle line only fires sometimes, so the
# sequence a reader would see depends on the data rather than on the source.
BRANCHING = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        if batch % 2:
            log.warning("batch %d: retrying", batch)
        log.debug("batch %d: committing", batch)
"""

# A body whose last line is an f-string. The template is destroyed at the call
# site, so `record.msg` arrives as rendered text and that source is unplaceable.
FSTRING = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        log.debug("batch %d: fetching manifest", batch)
        log.debug(f"batch {batch}: far wider than any real template in this body")
"""

# `phases`' announcement line: one call site, so position is 1 of 1 forever.
LONE = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.info("stage %d", batch)
"""

STAGES = (
    "batch %d: opening connection",
    "batch %d: fetching manifest",
    "batch %d: validating checksums",
)


def _module(tmp_path: Path, name: str, source: str) -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return str(path)


def _iterate(store, make_row, path, *, at, pace, stages=STAGES, upto=None):
    """One pass of the body: every stage in order, evenly spread over `pace`."""
    step = pace / len(stages)
    store.append(
        [
            make_row(
                pathname=path,
                lineno=8 + index,
                func_name="run",
                msg=template,
                created=at + index * step,
            )
            for index, template in enumerate(stages[: upto or len(stages)])
        ]
    )


def _run(store, make_row, path, *, pace, cycles=4, stages=STAGES):
    at = 100.0
    for _ in range(cycles):
        _iterate(store, make_row, path, at=at, pace=pace, stages=stages)
        at += pace
    return at


# --- the criterion ----------------------------------------------------------


def test_a_loop_too_slow_to_read_grows_a_position_row(store, make_row, tmp_path):
    """`sequence`. One iteration every three seconds says almost nothing about
    whether the program is alive; the stage it is on says it every second."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    _run(store, make_row, path, pace=3.0)
    (row,) = LoopRowModel(store, min_repeats=3).poll()
    assert row.position is not None
    assert (row.position.current, row.position.total) == (3, 3)


def test_a_fast_loop_stays_one_row(store, make_row, tmp_path):
    """`siblings`. The same structure at 10 iterations a second: the loop row
    is plainly moving, and a sub-iteration bar repainting from whichever of
    dozens of iterations a poll landed in would be noise."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    _run(store, make_row, path, pace=0.1, cycles=20)
    (row,) = LoopRowModel(store, min_repeats=3).poll()
    assert row.position is None


def test_the_threshold_is_the_only_difference_between_the_two(
    store, make_row, tmp_path
):
    """Same file, same body, same merge — only the pace differs, which is the
    whole of the rule and worth pinning against the constant itself."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    _run(store, make_row, path, pace=MIN_LEGIBLE_PERIOD * 1.2)
    (slow,) = LoopRowModel(store, min_repeats=3).poll()
    assert slow.position is not None

    store.evict(keep_last=0)
    _run(store, make_row, path, pace=MIN_LEGIBLE_PERIOD * 0.8)
    (fast,) = LoopRowModel(store, min_repeats=3).poll()
    assert fast.position is None


def test_a_row_that_earned_the_second_one_never_loses_it(store, make_row, tmp_path):
    """Admission is one-way, for the reason every other decision here is: a
    period wobbling across the threshold must not make a row appear and
    disappear. Keeping a row nobody needs is the cheaper wrong answer."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=8.0)
    (row,) = model.poll()
    assert row.position is not None

    # Polled per iteration, because the period estimate is smoothed: one poll
    # folding forty fast iterations in at once still reads mostly as the old
    # slow pace.
    for _ in range(40):
        _iterate(store, make_row, path, at=at, pace=0.05)
        at += 0.05
        (row,) = model.poll()
    assert row.rate is not None and row.rate > 1, "the loop did not actually speed up"
    assert row.position is not None


# --- what it points at ------------------------------------------------------


def test_the_position_follows_the_call_site_that_fired_last(store, make_row, tmp_path):
    """The ordinal is a dict lookup on `file:lineno`, so an iteration caught
    part-way through reads where it actually is rather than where a runtime
    counter had got to."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=3.0)
    assert model.poll()[0].position.current == 3

    _iterate(store, make_row, path, at=at, pace=3.0, upto=2)
    (row,) = model.poll()
    assert row.position.current == 2, "the bar did not reset with the iteration"


def test_the_stage_is_named_by_its_own_template(store, make_row, tmp_path):
    """The loop row is named for the loop, because it has several templates
    and no reason to prefer one. The position row is named for the *stage*,
    which has exactly one and is the thing that changes as the bar fills."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=3.0)
    assert model.poll()[0].position.label == "batch …: validating checksums"

    _iterate(store, make_row, path, at=at, pace=3.0, upto=1)
    assert model.poll()[0].position.label == "batch …: opening connection"


def test_the_stage_column_is_sized_for_every_stage_at_once(store, make_row, tmp_path):
    """A grid column is as wide as its widest cell, so a label sized to the
    current stage would drag every bar on screen sideways three times an
    iteration. The width covers the whole body, read from the AST, and is the
    same number before the first stage as after the last."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=3.0)
    widest = len("batch …: validating checksums")
    assert model.poll()[0].position.width == widest

    _iterate(store, make_row, path, at=at, pace=3.0, upto=1)
    assert model.poll()[0].position.width == widest


def test_an_fstring_stage_is_neither_named_nor_counted_in_the_width(
    store, make_row, tmp_path
):
    """An f-string leaves `record.msg` holding rendered text, which fails the
    drift guard, so that call site is unplaceable and can never be the stage on
    show. The row stays on the last line it can name — and the width is sized
    for the stages it can, not for the one it will never print."""
    path = _module(tmp_path, "fstring.py", FSTRING)
    stages = (
        "batch %d: opening connection",
        "batch %d: fetching manifest",
        "batch 3: far wider than any real template in this body",
    )
    model = LoopRowModel(store, min_repeats=3)
    _run(store, make_row, path, pace=3.0, stages=stages)

    position = model.poll()[0].position
    assert position is not None
    # The f-string fired most recently and is skipped, so the newest stage the
    # row can name is the second — of three, because the AST counts all three.
    assert (position.current, position.total) == (2, 3)
    assert position.label == "batch …: fetching manifest"
    # The widest of the two stages that can be named, not of the three lines.
    assert position.width == len("batch …: opening connection")
    assert position.width < len(stages[2])


# --- and where it refuses ---------------------------------------------------


def test_a_body_with_a_branch_gets_no_position_row(store, make_row, tmp_path):
    """`transform`'s conditional warning. The body emits a different sequence
    depending on the data, so a determinate bar over it would show a *wrong*
    percentage rather than an imprecise one — which Principle 10 does not
    license. The loop row itself is untouched."""
    path = _module(tmp_path, "branching.py", BRANCHING)
    store.append(
        [
            make_row(
                pathname=path,
                lineno=lineno,
                func_name="run",
                msg=msg,
                created=100.0 + cycle * 3.0 + offset,
            )
            for cycle in range(4)
            for lineno, msg, offset in (
                (8, "batch %d: opening connection", 0.0),
                (11, "batch %d: committing", 1.5),
            )
        ]
    )
    (row,) = LoopRowModel(store, min_repeats=3).poll()
    assert len(row.members) == 2, "the loop row itself should be unaffected"
    assert row.position is None


def test_a_body_with_one_call_site_gets_no_position_row(store, make_row, tmp_path):
    """`phases`' announcement line. `1 of 1` on every record is a row that
    cannot move, which fails the criterion that would have admitted it."""
    path = _module(tmp_path, "lone.py", LONE)
    store.append(
        [
            make_row(
                pathname=path,
                lineno=8,
                func_name="run",
                msg="stage %d",
                created=100.0 + i * 3.0,
            )
            for i in range(4)
        ]
    )
    (row,) = LoopRowModel(store, min_repeats=3).poll()
    assert row.position is None


def test_a_file_with_no_source_on_disk_gets_no_position_row(store, make_row):
    """The bare-install and generated-code path. There is no body to order, so
    there is no second row — and the first one draws exactly as it always did."""
    model = LoopRowModel(store, min_repeats=3)
    store.append(
        [
            make_row(pathname="<string>", msg="generated %d", created=100.0 + i * 3.0)
            for i in range(4)
        ]
    )
    (row,) = model.poll()
    assert row.count == 4
    assert row.position is None


def test_a_member_the_ast_could_not_place_is_skipped(tmp_path):
    """Driven directly, because `LoopRowModel` cannot produce this: a source
    whose site is unknown never joins a statically grouped row. It is the
    contract `CyclePositionModel` states for anyone else — a member with no
    ordinal is passed over rather than counted as stage zero."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    loop = SourceKey(path, 7, "run")
    placed = SourceKey(path, 8, "run")
    unplaced = SourceKey(path, 99, "run")
    structure = static.analyze_file(path)
    position = CyclePositionModel().of(
        placed,
        loop=loop,
        period=3.0,
        sites={placed: structure.call_sites[8], unplaced: None},
        # The unplaced source fired most recently, so it would win the "which
        # stage is current" comparison if it were eligible at all.
        states=[
            BarState(source=placed, count=4, period=3.0, last_at=100.0),
            BarState(source=unplaced, count=4, period=3.0, last_at=200.0),
        ],
        label_of=lambda source: f"line {source.lineno}",
    )
    assert position is not None
    assert (position.current, position.label) == (1, "line 8")


def test_an_edited_file_gets_no_position_row(store, make_row, tmp_path):
    """The drift guard, from this feature's side. A file edited since the
    running process imported it makes `file:lineno` point somewhere else, so
    the ordinal it would yield is somebody else's."""
    path = _module(tmp_path, "sequence.py", SEQUENCE)
    store.append(
        [
            make_row(
                pathname=path,
                lineno=8 + index,
                func_name="run",
                msg="nothing this file says",
                created=100.0 + cycle * 3.0 + index,
            )
            for cycle in range(4)
            for index in range(3)
        ]
    )
    for row in LoopRowModel(store, min_repeats=3).poll():
        assert row.position is None
