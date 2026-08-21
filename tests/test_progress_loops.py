"""One row per loop, not per call site: the display layer over the identity one.

No `rich` here — the row model is display-independent, so it runs on a bare
install like the rest of the models.

Two grouping paths, and both are exercised against real files rather than
mocks. The static path needs source on disk with templates that match the
records, so these write a small module to `tmp_path` and build rows keyed on
it; the runtime path is what `/nonexistent/foo.py` (`make_row`'s default, and a file
that does not exist) already gives.
"""

from __future__ import annotations

from fixture_sources import (
    CALLER,
    MIXED_BODY,
    NESTED,
    NOT_IN_A_LOOP,
    PHASES,
    SETTLED_CYCLES,
    SIBLINGS,
)
from lumberjack.renderers.progress import MAX_LABEL, LoopRowModel

# --- the runtime path -------------------------------------------------------
#
# `/nonexistent/foo.py` has no source on disk, so everything here falls back to what
# shipped before: equal periods plus a shared worker means one loop body.


def test_two_lines_at_one_pace_on_one_thread_become_one_row(store, make_row):
    model = LoopRowModel(store, min_repeats=3)
    for i in range(4):
        store.append(
            [
                make_row(lineno=6, msg="parsed %d", created=100.0 + i),
                make_row(lineno=7, msg="validated %d", created=100.05 + i),
            ]
        )
    (row,) = model.poll()
    assert len(row.members) == 2
    assert row.count == 4, "the row counts iterations, not the 8 records behind it"


def test_two_lines_on_two_threads_are_never_one_row(store, make_row):
    """The same check containment scoping needs, and load-bearing for the same
    reason: two loops on two threads can pace identically and share nothing."""
    model = LoopRowModel(store, min_repeats=3)
    for i in range(4):
        store.append(
            [
                make_row(lineno=6, thread=1, msg="alpha %d", created=100.0 + i),
                make_row(lineno=7, thread=2, msg="beta %d", created=100.05 + i),
            ]
        )
    assert len(model.poll()) == 2


def test_two_lines_at_different_paces_are_never_one_row(store, make_row):
    model = LoopRowModel(store, min_repeats=3)
    for i in range(4):
        store.append([make_row(lineno=6, msg="slow %d", created=100.0 + i * 10)])
        store.append([make_row(lineno=7, msg="fast %d", created=100.0 + i)])
    assert len(model.poll()) == 2


def test_an_untimed_source_is_never_merged(store, make_row):
    """No period is no evidence. Two sources that share nothing but the
    absence of a measurement are not one loop."""
    model = LoopRowModel(store, min_repeats=3)
    store.append([make_row(lineno=6, msg="a %d", created=100.0) for _ in range(4)])
    store.append([make_row(lineno=7, msg="b %d", created=100.0) for _ in range(4)])
    assert len(model.poll()) == 2


# --- what a row says --------------------------------------------------------


def test_a_single_site_row_is_labelled_with_its_template(store, make_row):
    model = LoopRowModel(store, min_repeats=3)
    store.append([make_row(msg="fetched row %d", created=100.0 + i) for i in range(4)])
    (row,) = model.poll()
    assert row.label == "fetched row …"


def test_a_row_with_no_template_falls_back_to_the_source_location(store, make_row):
    model = LoopRowModel(store, min_repeats=3)
    store.append([make_row(msg="", created=100.0 + i) for i in range(4)])
    (row,) = model.poll()
    assert row.label == "foo.py:10 bar()"


def test_a_label_does_not_change_when_the_message_does(store, make_row):
    """An f-string at the call site destroys the template, so `msg` is the
    *rendered* string and differs record to record. A label recomputed from it
    every poll would flicker; frozen, the row merely has a poor name."""
    model = LoopRowModel(store, min_repeats=3)
    store.append([make_row(msg=f"item {i}", created=100.0 + i) for i in range(4)])
    first = model.poll()[0].label
    store.append([make_row(msg=f"item {i}", created=110.0 + i) for i in range(4)])
    assert model.poll()[0].label == first


def test_the_count_is_the_busiest_member(store, make_row):
    """Merged call sites firing unequal numbers of times — a conditional error
    line in the body — make "iterations" ambiguous. A line that fires every
    iteration is a better clock than one that fires sometimes."""
    model = LoopRowModel(store, min_repeats=3)
    for i in range(6):
        rows = [make_row(lineno=6, msg="every %d", created=100.0 + i)]
        if i % 3 == 0:
            rows.append(make_row(lineno=7, msg="sometimes %d", created=100.02 + i))
        store.append(rows)
        model.poll()
    (row,) = model.poll()
    assert row.count == 6


def test_the_count_never_goes_backwards_after_eviction(store, make_row):
    """Max of monotone counts is monotone, which is what a progress bar has to
    mean: trimming the store must not rewrite the work already done."""
    model = LoopRowModel(store, min_repeats=3)
    store.append([make_row(msg="item %d", created=100.0 + i) for i in range(10)])
    assert model.poll()[0].count == 10
    store.evict(keep_last=2)
    assert model.poll()[0].count == 10


def test_the_identity_layer_still_counts_records(store, make_row):
    """`rows()` answers "how far along is this" and `bars()` answers "what did
    we capture". Merging is the display's business and must not touch the
    second — the store and the exit summary read it."""
    model = LoopRowModel(store, min_repeats=3)
    for i in range(4):
        store.append(
            [
                make_row(lineno=6, msg="parsed %d", created=100.0 + i),
                make_row(lineno=7, msg="validated %d", created=100.05 + i),
            ]
        )
    (row,) = model.poll()
    assert row.count == 4
    assert sorted(bar.count for bar in model.bars()) == [4, 4]
    assert sum(bar.count for bar in model.bars()) == 8


def test_a_rows_key_never_migrates_as_it_gains_members(store, make_row):
    """The key is the rich `Task`'s identity, and a `Task` recreated silently
    restarts the elapsed clock of a loop that has been running for minutes."""
    model = LoopRowModel(store, min_repeats=2)
    store.append(
        [make_row(lineno=6, msg="first %d", created=100.0 + i) for i in range(3)]
    )
    (row,) = model.poll()
    key = row.key

    for i in range(3, 8):
        store.append(
            [
                make_row(lineno=6, msg="first %d", created=100.0 + i),
                make_row(lineno=7, msg="second %d", created=100.05 + i),
            ]
        )
        model.poll()
    (row,) = model.poll()
    assert len(row.members) == 2
    assert row.key == key


# --- ordering and collapse --------------------------------------------------


def test_a_quiet_subtree_sinks_below_a_running_one(store, make_row):
    model = LoopRowModel(store, min_repeats=3, clock=lambda: 1_000.0)
    store.append(
        [make_row(lineno=6, msg="finished %d", created=100.0 + i) for i in range(4)]
    )
    model.poll()
    store.append(
        [
            make_row(lineno=9, msg="running %d", created=999.0 + i * 0.1)
            for i in range(4)
        ]
    )
    assert [row.label for row in model.poll()] == ["running …", "finished …"]


def test_quiet_rows_are_ordered_by_how_recently_they_moved(store, make_row):
    # Paces far enough apart not to read as one loop body, close enough not to
    # read as one nested in the other.
    model = LoopRowModel(store, min_repeats=3, clock=lambda: 1_000.0)
    store.append(
        [make_row(lineno=6, msg="first %d", created=100.0 + i) for i in range(4)]
    )
    model.poll()
    store.append(
        [make_row(lineno=9, msg="second %d", created=200.0 + i * 1.8) for i in range(4)]
    )
    assert [row.label for row in model.poll()] == ["second …", "first …"]


def test_a_row_is_live_while_any_of_its_call_sites_is(store, make_row):
    """A body whose last line is conditional must not retire the loop between
    one iteration's first line and the next's."""
    model = LoopRowModel(store, min_repeats=3, clock=lambda: 140.0)
    for i in range(4):
        store.append(
            [
                make_row(lineno=6, msg="old %d", created=100.0 + i),
                make_row(lineno=7, msg="new %d", created=100.05 + i),
            ]
        )
    model.poll()
    store.append([make_row(lineno=7, msg="new %d", created=139.9)])
    (row,) = model.poll()
    assert not row.idle


# --- the static path --------------------------------------------------------


def test_siblings_in_one_loop_body_merge_even_at_different_paces(
    store, make_row, write_module
):
    """What the AST buys over the timing: these two lines are one loop body by
    inspection, so they merge whatever their measured periods do. The runtime
    fallback would need them within 15% of each other."""
    path = str(write_module(SIBLINGS, name="siblings.py", strip=False))
    for i in range(4):
        store.append(
            [
                make_row(
                    pathname=path,
                    lineno=8,
                    func_name="run",
                    msg="row %d: parsed",
                    created=100.0 + i,
                ),
                make_row(
                    pathname=path,
                    lineno=9,
                    func_name="run",
                    msg="row %d: validated",
                    # Nowhere near the sibling's pace, on purpose.
                    created=100.0 + i * 3,
                ),
            ]
        )
    model = LoopRowModel(store, min_repeats=3)
    (row,) = model.poll()
    assert len(row.members) == 2
    assert row.label == "siblings.py:7 run()"


def test_a_lexically_nested_loop_is_placed_under_its_parent_at_birth(
    store, make_row, write_module
):
    """Place-at-birth. The nesting is in the source, so the child does not have
    to sit somewhere wrong until the period ratio confirms it."""
    path = str(write_module(NESTED, name="nested.py", strip=False))
    at = 100.0
    model = LoopRowModel(store, min_repeats=3)
    for _ in range(3):
        rows = [
            make_row(
                pathname=path,
                lineno=8,
                func_name="reconcile",
                msg="reconciling batch %d",
                created=at,
            )
        ]
        rows += [
            make_row(
                pathname=path,
                lineno=10,
                func_name="reconcile",
                msg="compared row %d against ledger",
                created=at + i * 0.4,
            )
            for i in range(20)
        ]
        store.append(rows)
        at += 8.0
        rows_drawn = model.poll()
    by_label = {row.label: row for row in rows_drawn}
    child = by_label["compared row … against ledger"]
    parent = by_label["reconciling batch …"]
    assert child.parent == parent.key
    assert child.depth == 1
    assert [row.label for row in rows_drawn] == [parent.label, child.label]


def test_a_lexically_corroborated_total_survives(store, make_row, write_module):
    """`pipeline`'s 20/20. Static says the containment is real, so the ratio
    the timing measured is allowed to stand as the inner loop's length."""
    path = str(write_module(NESTED, name="nested.py", strip=False))
    at = 100.0
    model = LoopRowModel(store, min_repeats=3)
    for _ in range(SETTLED_CYCLES):
        rows = [
            make_row(
                pathname=path,
                lineno=8,
                func_name="reconcile",
                msg="reconciling batch %d",
                created=at,
            )
        ]
        rows += [
            make_row(
                pathname=path,
                lineno=10,
                func_name="reconcile",
                msg="compared row %d against ledger",
                created=at + i * 0.4,
            )
            for i in range(20)
        ]
        store.append(rows)
        at += 8.0
        drawn = model.poll()
    child = next(r for r in drawn if r.label.startswith("compared"))
    assert child.total == 20


def test_a_cross_function_parent_keeps_the_indent_and_loses_the_total(
    store, make_row, write_module
):
    """`phases`. Period ordering cannot tell "A encloses B" from "A precedes
    B", and hands the running stage a total taken from an announcement line's
    period. The AST shows the child is a top-level loop in another function, so
    the claimed parent cannot lexically enclose it — the number goes, the
    indent stays, because the stage function really is called from in there.
    """
    path = str(write_module(PHASES, name="phases.py", strip=False))
    at = 100.0
    model = LoopRowModel(store, min_repeats=3)
    for _ in range(SETTLED_CYCLES):
        rows = [
            make_row(
                pathname=path,
                lineno=13,
                func_name="run",
                msg="stage %d",
                created=at,
            )
        ]
        rows += [
            make_row(
                pathname=path,
                lineno=8,
                func_name="write",
                msg="wrote partition %d",
                created=at + i * 0.4,
            )
            for i in range(20)
        ]
        store.append(rows)
        at += 8.0
        drawn = model.poll()
    stage = next(r for r in drawn if r.label.startswith("stage"))
    child = next(r for r in drawn if r.label.startswith("wrote"))
    assert child.parent == stage.key, "the indent is not the part that was wrong"
    assert child.depth == 1
    assert child.total is None, "a fabricated total survived the veto"
    assert not child.is_determinate


def test_a_parent_in_another_file_loses_the_total_too(store, make_row, write_module):
    """A different file is the same refusal as a different function, only more
    so — and comparing loop line numbers across two files would let one file's
    `run()` corroborate another's by coincidence of numbering."""
    caller = str(write_module(CALLER, name="caller.py", strip=False))
    worker = str(write_module(PHASES, name="phases.py", strip=False))
    at = 100.0
    model = LoopRowModel(store, min_repeats=3)
    for _ in range(SETTLED_CYCLES):
        rows = [
            make_row(
                pathname=caller,
                lineno=8,
                func_name="run",
                msg="stage %d",
                created=at,
            )
        ]
        rows += [
            make_row(
                pathname=worker,
                lineno=8,
                func_name="write",
                msg="wrote partition %d",
                created=at + i * 0.4,
            )
            for i in range(20)
        ]
        store.append(rows)
        at += 8.0
        drawn = model.poll()
    child = next(r for r in drawn if r.label.startswith("wrote"))
    assert child.depth == 1, "the indent is not the part that was wrong"
    assert child.total is None


def test_an_edited_file_is_ignored_rather_than_believed(store, make_row, write_module):
    """The drift guard. `file:lineno` describes the code the running process
    imported, and a file edited since points somewhere else entirely — so every
    structural claim keyed on it is wrong. A template that does not match is a
    refusal, and the runtime path takes over."""
    path = str(write_module(SIBLINGS, name="siblings.py", strip=False))
    for i in range(4):
        store.append(
            [
                make_row(
                    pathname=path,
                    lineno=8,
                    func_name="run",
                    msg="something else entirely",
                    created=100.0 + i,
                ),
                make_row(
                    pathname=path,
                    lineno=9,
                    func_name="run",
                    msg="and another thing",
                    created=100.0 + i * 3,
                ),
            ]
        )
    model = LoopRowModel(store, min_repeats=3)
    # Two rows, not one: without static structure the periods are three
    # seconds apart and the runtime rule refuses to merge them.
    assert len(model.poll()) == 2


def test_a_line_that_moved_between_functions_is_refused(store, make_row, write_module):
    """The drift case the template check happens not to catch: the line still
    holds the same template, but `funcName` says the running code had it
    somewhere else. Both halves of the identity have to agree."""
    path = str(write_module(SIBLINGS, name="siblings.py", strip=False))
    store.append(
        [
            make_row(
                pathname=path,
                lineno=8,
                func_name="somewhere_else",
                msg="row %d: parsed",
                created=100.0 + i,
            )
            for i in range(4)
        ]
    )
    model = LoopRowModel(store, min_repeats=3)
    (row,) = model.poll()
    # Grouped and labelled by the runtime path, which claims nothing about the
    # file: had the AST been believed, the label would name the loop at line 7.
    assert row.label == "row …: parsed"


def test_a_repeating_line_outside_any_loop_is_its_own_row(
    store, make_row, write_module
):
    """A helper called from a loop somewhere else. The AST is exact about there
    being no loop here, which is a row on its own rather than a merge
    candidate — a caller's loop is not this line's body."""
    path = str(write_module(NOT_IN_A_LOOP, name="helper.py", strip=False))
    store.append(
        [
            make_row(
                pathname=path,
                lineno=7,
                func_name="emit",
                msg="emitted %d",
                created=100.0 + i,
            )
            for i in range(4)
        ]
    )
    model = LoopRowModel(store, min_repeats=3)
    (row,) = model.poll()
    assert row.label == "emitted …"
    assert row.members == (row.key,)


def test_an_untimed_row_is_not_a_merge_candidate_for_a_timed_one(store, make_row):
    """The other side of "no period is no evidence": a source already grouped
    by the runtime path with no measurement cannot absorb a later one."""
    model = LoopRowModel(store, min_repeats=3)
    store.append(
        [make_row(lineno=6, msg="untimed %d", created=100.0) for _ in range(4)]
    )
    model.poll()
    store.append(
        [make_row(lineno=7, msg="timed %d", created=200.0 + i) for i in range(4)]
    )
    assert len(model.poll()) == 2


# --- when the template is slow to turn up -----------------------------------
#
# Grouping is frozen once made, so a source whose template has not been
# harvested yet waits rather than committing to the runtime path it can never
# leave. The harvest reads the store's tail, so "not yet" means "buried under
# other traffic", and these two drive that from both ends.


def _bury(store, make_row, path: str, count: int, at: float) -> None:
    """`count` records from `write:8`, on top of whatever is already stored."""
    store.append(
        [
            make_row(
                pathname=path,
                lineno=8,
                func_name="write",
                msg="wrote partition %d",
                created=at + i * 0.01,
            )
            for i in range(count)
        ]
    )


def test_a_source_buried_under_traffic_waits_for_its_template(
    store, make_row, write_module
):
    path = str(write_module(PHASES, name="phases.py", strip=False))
    store.append(
        [
            make_row(
                pathname=path,
                lineno=13,
                func_name="run",
                msg="stage %d",
                created=100.0 + i,
            )
            for i in range(4)
        ]
    )
    _bury(store, make_row, path, 200, at=110.0)

    model = LoopRowModel(store, min_repeats=3)
    labels = [row.label for row in model.poll()]
    assert labels == ["wrote partition …"], "the buried source was grouped blind"
    # Next poll, next lookback: the template turns up and the row appears with
    # the label it was always going to have.
    assert sorted(row.label for row in model.poll()) == [
        "stage …",
        "wrote partition …",
    ]


def test_a_source_that_never_turns_up_is_written_off(store, make_row, write_module):
    """Escalating lookbacks terminate. Without the give-up a source that
    qualified in a burst and then went quiet would cost a store read on every
    redraw for the rest of the run."""
    path = str(write_module(PHASES, name="phases.py", strip=False))
    store.append(
        [
            make_row(
                pathname=path,
                lineno=13,
                func_name="run",
                msg="stage %d",
                created=100.0 + i,
            )
            for i in range(4)
        ]
    )
    _bury(store, make_row, path, 1_100, at=110.0)

    model = LoopRowModel(store, min_repeats=3)
    reads = 0
    real = store.recent

    def counting(*args, **kwargs):
        nonlocal reads
        reads += 1
        return real(*args, **kwargs)

    for _ in range(3):
        model.poll()
    store.recent = counting
    labels = sorted(row.label for row in model.poll())
    # Written off, so it is grouped by the runtime path and labelled with the
    # location rather than a template it never got.
    assert labels == ["phases.py:13 run()", "wrote partition …"]
    # The heartbeat reads the tail for its message; the template harvest must
    # not still be reading it too.
    assert reads <= 1, "a written-off source is still costing a store read"


def test_a_total_measured_against_a_sibling_is_not_reused_for_the_parent(
    store, make_row, write_module
):
    """The disagree-and-merge quadrant: static grouping and runtime containment
    both correct, and the composition of them wrong.

    Two lines share the inner body, so they merge into one row whose lexical
    parent is the outer loop. But the runtime model freezes containment for the
    busy line against the *conditional sibling* — the nearest slower same-worker
    level — so its `total` is the ratio to a source that is now inside this very
    row. Substituting the lexical parent and asking only whether that parent
    encloses the child launders that number into a confident, wrong percentage.

    It cannot self-correct either: `cycle_current` rebases on the sibling's
    firings, so the count never overruns the total and the pulse-withdrawal
    path that catches every other bad estimate never fires. A row that claims
    less is always allowed; a row that claims wrong is not.
    """
    path = str(write_module(MIXED_BODY, name="mixed.py", strip=False))
    at = 100.0
    model = LoopRowModel(store, min_repeats=3)
    for _ in range(SETTLED_CYCLES):
        rows = [
            make_row(
                pathname=path, lineno=8, func_name="process", msg="batch %d", created=at
            )
        ]
        for i in range(9):
            rows.append(
                make_row(
                    pathname=path,
                    lineno=10,
                    func_name="process",
                    msg="row %d",
                    created=at + i * 1.0,
                )
            )
            if i % 3 == 0:
                rows.append(
                    make_row(
                        pathname=path,
                        lineno=12,
                        func_name="process",
                        msg="checkpoint %d",
                        created=at + i * 1.0,
                    )
                )
        store.append(rows)
        at += 9.0
        drawn = model.poll()

    inner = next(row for row in drawn if row.depth == 1)
    outer = next(row for row in drawn if row.depth == 0)

    assert inner.parent == outer.key, "the lexical parent is still the right parent"
    assert len(inner.members) == 2, "the two inner lines still merge into one row"
    assert inner.total is None, (
        "a total measured against a source inside this very row was reused as "
        "the fraction of a different parent"
    )


# --- every branch is bounded, not just the template one (#99) ---------------
#
# A label shares one line with a bar, and the bar is the row's reason to
# exist. `describe_template` clipped its own result from the start; the other
# three branches returned whatever the file, the function or the loop
# statement happened to be called, and those are precisely the rows that have
# *no* template — so they were both the longest labels and the least
# informative ones.

_LONG_FUNC = "normalise_and_validate_every_incoming_payload_before_writing_it"
_LONG_FILE = "extraordinarily_long_module_name_for_a_data_pipeline_stage.py"


def test_a_single_site_row_with_no_template_is_clipped(store, make_row):
    """The `key.format()` branch: `file:line func()`, none of it bounded."""
    model = LoopRowModel(store, min_repeats=3)
    store.append(
        [
            make_row(
                msg="",
                pathname=f"/nonexistent/{_LONG_FILE}",
                func_name=_LONG_FUNC,
                created=100.0 + i,
            )
            for i in range(4)
        ]
    )
    (row,) = model.poll()
    assert len(row.label) <= MAX_LABEL, row.label
    assert row.label.endswith("…")


def test_a_runtime_merged_row_is_clipped(store, make_row):
    """The `f"{base} {func}()"` branch: no source on disk, so no loop to name."""
    model = LoopRowModel(store, min_repeats=3)
    for i in range(4):
        store.append(
            [
                make_row(
                    lineno=lineno,
                    pathname=f"/nonexistent/{_LONG_FILE}",
                    func_name=_LONG_FUNC,
                    msg="",
                    created=100.0 + i,
                )
                for lineno in (10, 11)
            ]
        )
    (row,) = model.poll()
    assert len(row.members) == 2
    assert len(row.label) <= MAX_LABEL, row.label


def test_a_statically_merged_row_is_clipped(store, make_row, write_module):
    """The `group.at.format()` branch: the loop statement's own location."""
    path = str(write_module(SIBLINGS, name=_LONG_FILE, strip=False))
    model = LoopRowModel(store, min_repeats=3)
    at = 100.0
    for _ in range(4):
        store.append(
            [
                make_row(
                    pathname=path, lineno=8, func_name="run", msg="row %d: parsed"
                ),
                make_row(
                    pathname=path,
                    lineno=9,
                    func_name="run",
                    msg="row %d: validated",
                ),
            ]
        )
        at += 1.0
        rows = model.poll()
    (row,) = rows
    assert len(row.members) == 2
    assert len(row.label) <= MAX_LABEL, row.label


def test_a_long_template_is_still_clipped(store, make_row):
    """The branch that was always bounded, kept honest beside the other three."""
    model = LoopRowModel(store, min_repeats=3)
    long_template = "reconciling %s against the ledger for tenant %s in region %s"
    store.append([make_row(msg=long_template, created=100.0 + i) for i in range(4)])
    (row,) = model.poll()
    assert len(row.label) <= MAX_LABEL, row.label
