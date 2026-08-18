"""The second row: where a slow loop is within its current iteration (#53).

No `rich` here either — deciding whether the row is earned, and what it points
at, is model work and runs on a bare install.

Every fixture writes a real module via `write_module` and logs records keyed on
it, because the whole feature rests on reading that file: the ordinal comes
from the AST's view of the body, not from anything counted at runtime. A test
that mocked the structure out would be testing nothing.
"""

from __future__ import annotations

import pytest

from fixture_sources import (
    BRANCHING,
    FSTRING,
    LONE,
    ONE_NAMEABLE,
    SEQUENCE,
    STAGES,
)
from lumberjack import static
from lumberjack.renderers.progress import (
    MIN_LEGIBLE_PERIOD,
    BarState,
    CyclePositionModel,
    LoopRowModel,
)
from lumberjack.schema import SourceKey


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


@pytest.mark.parametrize(
    ("pace", "cycles", "expects_row"),
    [
        # `sequence`: one iteration every three seconds says almost nothing
        # about whether the program is alive; the stage it is on says it
        # every second.
        (3.0, 4, True),
        # `siblings` at ten iterations a second: the loop row is plainly
        # moving, and a sub-iteration bar repainting from whichever of dozens
        # of iterations a poll landed in would be noise.
        (0.1, 20, False),
        # Pinned against the constant itself, on both sides of it — the whole
        # rule is that pace is the only thing that matters.
        (MIN_LEGIBLE_PERIOD * 1.2, 4, True),
        (MIN_LEGIBLE_PERIOD * 0.8, 4, False),
    ],
)
def test_a_position_row_is_earned_only_below_the_legible_period(
    store, make_row, write_module, pace, cycles, expects_row
):
    """Same file, same body, same merge — only the pace differs, which is the
    whole of the rule for whether the second row is earned."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
    _run(store, make_row, path, pace=pace, cycles=cycles)
    (row,) = LoopRowModel(store, min_repeats=3).poll()
    assert (row.position is not None) == expects_row
    if row.position is not None:
        assert (row.position.current, row.position.total) == (3, 3)


def test_a_row_that_earned_the_second_one_never_loses_it(store, make_row, write_module):
    """Admission is one-way, for the reason every other decision here is: a
    period wobbling across the threshold must not make a row appear and
    disappear. Keeping a row nobody needs is the cheaper wrong answer."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
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


def test_the_position_follows_the_call_site_that_fired_last(
    store, make_row, write_module
):
    """The ordinal is a dict lookup on `file:lineno`, so an iteration caught
    part-way through reads where it actually is rather than where a runtime
    counter had got to."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=3.0)
    assert model.poll()[0].position.current == 3

    _iterate(store, make_row, path, at=at, pace=3.0, upto=2)
    (row,) = model.poll()
    assert row.position.current == 2, "the bar did not reset with the iteration"


def test_the_stage_is_named_by_its_own_template(store, make_row, write_module):
    """The loop row is named for the loop, because it has several templates
    and no reason to prefer one. The position row is named for the *stage*,
    which has exactly one and is the thing that changes as the bar fills."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=3.0)
    assert model.poll()[0].position.label == "batch …: validating checksums"

    _iterate(store, make_row, path, at=at, pace=3.0, upto=1)
    assert model.poll()[0].position.label == "batch …: opening connection"


def test_the_stage_column_is_sized_for_every_stage_at_once(
    store, make_row, write_module
):
    """A grid column is as wide as its widest cell, so a label sized to the
    current stage would drag every bar on screen sideways three times an
    iteration. The width covers the whole body, read from the AST, and is the
    same number before the first stage as after the last."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
    model = LoopRowModel(store, min_repeats=3)
    at = _run(store, make_row, path, pace=3.0)
    widest = len("batch …: validating checksums")
    assert model.poll()[0].position.width == widest

    _iterate(store, make_row, path, at=at, pace=3.0, upto=1)
    assert model.poll()[0].position.width == widest


def test_an_fstring_stage_is_neither_named_nor_counted_in_the_width(
    store, make_row, write_module
):
    """An f-string leaves `record.msg` holding rendered text, which fails the
    drift guard, so that call site is unplaceable and can never be the stage on
    show. The row stays on the last line it can name — and the width is sized
    for the stages it can, not for the one it will never print."""
    path = str(write_module(FSTRING, name="fstring.py", strip=False))
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


def test_a_body_with_a_branch_gets_no_position_row(store, make_row, write_module):
    """`transform`'s conditional warning. The body emits a different sequence
    depending on the data, so a determinate bar over it would show a *wrong*
    percentage rather than an imprecise one — which Principle 10 does not
    license. The loop row itself is untouched."""
    path = str(write_module(BRANCHING, name="branching.py", strip=False))
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


def test_a_body_with_one_call_site_gets_no_position_row(store, make_row, write_module):
    """`phases`' announcement line. `1 of 1` on every record is a row that
    cannot move, which fails the criterion that would have admitted it."""
    path = str(write_module(LONE, name="lone.py", strip=False))
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


def test_a_body_whose_second_member_can_never_be_named_gets_no_row(
    store, make_row, write_module
):
    """`LONE` wearing a second line, and the gap issue #83 reported.

    The AST counts two call sites, so the minimum was satisfied and the row
    was admitted — but an f-string leaves `record.msg` holding rendered text,
    which the drift guard refuses, so that member can never be the stage on
    show. The result was `1 of 2` forever: a determinate bar that cannot
    reach its own total, which fails the legibility criterion that admitted
    it exactly as `1 of 1` does.
    """
    path = str(write_module(ONE_NAMEABLE, name="one_nameable.py", strip=False))
    store.append(
        [
            make_row(
                pathname=path,
                lineno=8,
                func_name="run",
                msg="batch %d: opening connection",
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


def test_a_member_the_ast_could_not_place_is_skipped(write_module):
    """Driven directly, because `LoopRowModel` cannot produce this: a source
    whose site is unknown never joins a statically grouped row. It is the
    contract `CyclePositionModel` states for anyone else — a member with no
    ordinal is passed over rather than counted as stage zero."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
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


def test_an_edited_file_gets_no_position_row(store, make_row, write_module):
    """The drift guard, from this feature's side. A file edited since the
    running process imported it makes `file:lineno` point somewhere else, so
    the ordinal it would yield is somebody else's."""
    path = str(write_module(SEQUENCE, name="sequence.py", strip=False))
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
