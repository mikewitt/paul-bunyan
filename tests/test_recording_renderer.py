"""The recorder is a real `Renderer`, and it records the real decisions.

Two claims, and they need separate tests. That it satisfies the `Renderer`
Protocol — including the parts of the contract that are about *misuse*: a
second `close()`, a record arriving after one, concurrent `render()` calls
from several threads. And that what it records is the same answer the rich
renderer paints, which is checked by driving both over the same store and
comparing, so a recorder that quietly grew its own opinion fails here.

Rich-free except for the one test that is about agreeing with rich.
"""

from __future__ import annotations

import logging
import threading

import pytest

from lumberjack.handler import LumberjackHandler
from lumberjack.renderers import Renderer
from lumberjack.schema import LogRecordRow
from lumberjack.store import RecordStore
from recording_renderer import RecordingRenderer

#: Tier 2 — a component contract, driven through a public component API.
#: See tests/README.md; `test_tier2_rules.py` checks what the mark claims.
pytestmark = pytest.mark.tier2


def test_it_is_a_renderer():
    """Structural conformance, which is all `init()` requires."""
    assert isinstance(RecordingRenderer, type)


def test_it_satisfies_the_renderer_protocol(store: RecordStore):
    assert isinstance(RecordingRenderer(store), Renderer)


def test_a_lossy_renderer_declares_itself(store: RecordStore):
    """Teardown's exit dump recovers what a lossy display swallowed, and
    reads this flag by `getattr` to decide whether to bother."""
    assert RecordingRenderer(store).write_through is False


def test_closing_twice_is_a_no_op(store: RecordStore):
    renderer = RecordingRenderer(store)
    renderer.close()
    renderer.close()
    assert renderer.closed


def test_a_record_arriving_after_close_does_not_raise(store: RecordStore, make_row):
    """`shutdown()` closes the display *before* it removes the handler, so a
    thread logging in that window still arrives — and raising would surface
    as an exception from an ordinary `logging` call in code that has never
    heard of lumberjack."""
    renderer = RecordingRenderer(store)
    renderer.close()
    renderer.render(make_row())
    assert renderer.rendered == ()


def test_refreshing_after_close_plans_nothing(store: RecordStore, make_row):
    renderer = RecordingRenderer(store)
    store.append([make_row(created=100.0 + i) for i in range(4)])
    renderer.close()
    renderer.refresh()
    assert renderer.frames == ()


def test_render_is_safe_from_several_threads(store: RecordStore, make_row):
    """It is called from whichever thread logged, with no serialisation of
    its own — the handler's lock guards the buffer, not this."""
    renderer = RecordingRenderer(store)
    rows = [make_row(created=100.0 + i) for i in range(200)]

    def feed(chunk: list[LogRecordRow]) -> None:
        for row in chunk:
            renderer.render(row)

    threads = [threading.Thread(target=feed, args=(rows[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(renderer.rendered) == 200


# --- what it records --------------------------------------------------------


def test_a_repeating_line_becomes_one_row_counting_iterations(
    store: RecordStore, make_row
):
    """The premise, asserted as a number rather than grepped out of a frame."""
    renderer = RecordingRenderer(store, min_repeats=3)
    store.append([make_row(msg="parsed %d", created=100.0 + i) for i in range(50)])
    renderer.refresh()

    counts = renderer.counts()
    assert (counts.sources, counts.loops) == (1, 1)
    frame = renderer.frame
    assert frame is not None
    row = frame.of("parsed …")
    assert row is not None
    assert row.detail == "50 iterations"


def test_sibling_call_sites_are_one_row_and_several_sources(
    store: RecordStore, make_row
):
    """The display unit is not the identity unit, and this is the number that
    says so: four call sites narrating one loop are one row, and the row
    counts iterations rather than the records behind them."""
    renderer = RecordingRenderer(store, min_repeats=3)
    for cycle in range(40):
        store.append(
            [
                make_row(lineno=8 + i, msg=f"row %d: stage {i}", created=100.0 + cycle)
                for i in range(4)
            ]
        )
    renderer.refresh()

    counts = renderer.counts()
    assert counts.sources == 4, "four call sites"
    assert counts.loops == 1, "one loop"
    frame = renderer.frame
    assert frame is not None
    (row,) = frame.rows
    assert row.detail == "40 iterations", "not 160 records"


def test_every_frame_is_kept_so_a_row_can_be_watched_moving(
    store: RecordStore, make_row
):
    renderer = RecordingRenderer(store, min_repeats=3)
    for cycle in range(6):
        store.append([make_row(msg="parsed %d", created=100.0 + cycle)])
        renderer.refresh()
    assert len(renderer.frames) == 6
    details = [
        row.detail
        for row in (frame.of("parsed …") for frame in renderer.frames)
        if row is not None
    ]
    assert details == ["3 iterations", "4 iterations", "5 iterations", "6 iterations"]


def test_the_ceiling_is_visible_as_a_number(store: RecordStore, make_row):
    renderer = RecordingRenderer(store, min_repeats=3, max_bars=2)
    # One thread each: two loops on one worker with the same period are one
    # merged row, which is the row model working and not what this pins.
    for line in range(5):
        store.append(
            [
                make_row(
                    lineno=10 + line,
                    thread=line,
                    msg=f"loop {line} %d",
                    created=100.0 + i,
                )
                for i in range(4)
            ]
        )
    renderer.refresh()
    counts = renderer.counts()
    assert (counts.loops, counts.drawn_loops, counts.suppressed_loops) == (5, 2, 3)


def test_it_reads_the_store_rather_than_the_records_handed_to_it(
    store: RecordStore, make_row
):
    """The counts come from the store, which is what makes them survive a
    renderer that was attached late — and what makes `rendered` a different
    number from `counts().sources` rather than a duplicate of it."""
    renderer = RecordingRenderer(store, min_repeats=3)
    store.append([make_row(msg="parsed %d", created=100.0 + i) for i in range(10)])
    renderer.refresh()
    assert renderer.rendered == ()
    assert renderer.counts().sources == 1


def test_it_works_through_the_real_capture_path(store: RecordStore):
    """End to end on the write path: stdlib `logging` → handler → store → the
    frame, with no fabricated rows anywhere."""
    renderer = RecordingRenderer(store, min_repeats=3)
    handler = LumberjackHandler(on_record=renderer.render)
    logger = logging.getLogger("recording-renderer-test")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(20):
            logger.debug("processing item %d", i)
    finally:
        logger.removeHandler(handler)
    store.append(handler.drain())
    renderer.refresh()

    assert len(renderer.rendered) == 20, "every record reached the write-through path"
    frame = renderer.frame
    assert frame is not None
    row = frame.of("processing item …")
    assert row is not None
    assert row.detail == "20 iterations"


# --- and that it agrees with the renderer that actually draws ---------------


def test_it_records_what_the_rich_renderer_paints(store: RecordStore, make_row):
    """The recorder is not a second implementation, and this is what keeps it
    honest: both are driven over one store and their frames compared.

    It would be cheaper to assert this by construction — they call the same
    `plan_frame()` — but "the code is shared" is a claim about today's code,
    and this is a test about tomorrow's.
    """
    pytest.importorskip("rich")
    import io

    from lumberjack.renderers.rich_renderer import RichProgressRenderer

    recorder = RecordingRenderer(store, min_repeats=3)
    rich = RichProgressRenderer(
        store, stream=io.StringIO(), min_repeats=3, refresh_interval=0
    )
    try:
        for cycle in range(8):
            store.append(
                [
                    make_row(
                        lineno=8, msg="reconciling batch %d", created=100.0 + cycle
                    ),
                    *(
                        make_row(
                            lineno=10,
                            msg="compared row %d",
                            created=100.0 + cycle + 0.01 * i,
                        )
                        for i in range(1, 21)
                    ),
                ]
            )
            recorder.refresh()
            rich.refresh()
            assert recorder.frame == rich.frame, f"cycle {cycle}"
    finally:
        rich.close()
