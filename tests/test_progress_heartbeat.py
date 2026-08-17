"""Tests for the session heartbeat: is anything arriving at all?

The element for a program whose lines never repeat, and the one the two
fixtures below exist for: `oneshot` (six startup lines, one each) and
`silent` (a line, three seconds of real work, a line) both drew literally
nothing before this, which is indistinguishable from hung.

Everything here is a statement about records that arrived. The first two
tests are the guard on that: nothing in this row may move on wall-clock
time, because a frame that turns while the stream said nothing is claiming
liveness nobody observed.

Driven through `RepeatingSourceModel` rather than `SessionHeartbeat` directly,
because that is how the heartbeat is fed: it rides on the source model's poll
rather than querying the store for a delta of its own.
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

from lumberjack.renderers.progress import (
    HEARTBEAT_FRAMES,
    MESSAGE_LOOKBACK,
    RepeatingSourceModel,
    SessionHeartbeat,
)


def _loop_rows(make_row, n: int, **overrides):
    """N records from one source location, the way a loop emits them."""
    return [make_row(message=f"item {i}", **overrides) for i in range(n)]


def test_the_glyph_is_frozen_across_empty_polls(store, make_row):
    """The honesty test, and the reason rich's `Spinner` is not used here:
    `Spinner.render()` produces four distinct frames from wall-clock alone,
    with no data passed to it at all. This row's frame is an index that only
    a record moves."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 4))
    model.poll()
    running = model.heartbeat.glyph

    for _ in range(20):
        model.poll()
        assert model.heartbeat.glyph == running, "the heartbeat animated on its own"


def test_the_beat_advances_only_when_records_arrived(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    beats = []
    for arriving in (True, False, False, True, False, True):
        if arriving:
            store.append(_loop_rows(make_row, 2))
        model.poll()
        beats.append(model.heartbeat.beat)
    assert beats == [1, 1, 1, 2, 2, 3]


def test_consecutive_beats_show_different_glyphs(store, make_row):
    """The other half of the freeze: a frame that never moves is as useless
    as one that always does."""
    model = RepeatingSourceModel(store, min_repeats=3)
    seen = []
    for _ in range(3):
        store.append(_loop_rows(make_row, 1))
        model.poll()
        seen.append(model.heartbeat.glyph)
    assert len(set(seen)) == 3


def test_the_frames_cycle_rather_than_running_out(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    for _ in range(len(HEARTBEAT_FRAMES) + 1):
        store.append(_loop_rows(make_row, 1))
        model.poll()
    assert model.heartbeat.glyph == HEARTBEAT_FRAMES[1]


def test_nothing_has_arrived_yet_is_an_empty_heartbeat(store):
    """Which is what stops the display drawing the row at all: a line reading
    "0 events" is true and worth nothing."""
    model = RepeatingSourceModel(store, min_repeats=3)
    model.poll()
    state = model.heartbeat
    assert (state.events, state.beat, state.rate, state.message) == (0, 0, None, None)


def test_the_heartbeat_counts_records_across_every_source(store, make_row):
    """`oneshot`'s shape: six sources, one record each, no source repeating
    often enough to earn a bar. The session still has something to say."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(lineno=200 + i) for i in range(6)])
    assert model.poll() == [], "a one-off line must not earn a bar"
    assert model.heartbeat.events == 6


def test_the_count_accumulates_across_polls(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 4))
    model.poll()
    store.append(_loop_rows(make_row, 3))
    model.poll()
    assert model.heartbeat.events == 7


def test_the_count_never_goes_backwards_after_eviction(store, make_row):
    """Monotonic by construction, as a bar's count is: the store is a window
    on the last N records, and trimming it must not make the session look
    less busy than it was."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 10))
    model.poll()
    store.evict(keep_last=2)
    model.poll()
    assert model.heartbeat.events == 10


def test_the_rate_comes_from_the_records_own_timestamps(store, make_row):
    store.append([make_row(created=100.0 + i * 0.5) for i in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3)
    model.poll()
    assert model.heartbeat.rate == pytest.approx(2.0)


def test_one_record_claims_no_rate(store, make_row):
    """One record establishes no interval, and a made-up number is worse than
    none — the same rule a source bar's rate follows."""
    store.append([make_row(created=100.0)])
    model = RepeatingSourceModel(store, min_repeats=3)
    model.poll()
    assert model.heartbeat.events == 1 and model.heartbeat.rate is None


def test_the_rate_spans_the_quiet_between_polls(store, make_row):
    """`silent`'s shape. Three seconds of real work between two lines is a
    slow session, and the rate has to say so rather than measuring only the
    bursts."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(created=100.0)])
    for _ in range(15):
        model.poll()  # the three silent seconds, one redraw at a time
    frozen = model.heartbeat
    store.append([make_row(created=103.0)])
    model.poll()

    assert frozen.beat == model.heartbeat.beat - 1, "the silence moved the beat"
    assert model.heartbeat.rate == pytest.approx(1 / 3)


def test_records_sharing_a_timestamp_claim_no_rate(store, make_row):
    store.append([make_row(created=100.0) for _ in range(5)])
    model = RepeatingSourceModel(store, min_repeats=3)
    model.poll()
    assert model.heartbeat.rate is None


def test_the_heartbeat_shows_the_newest_message(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(message="warming schema cache")])
    model.poll()
    assert model.heartbeat.message == "warming schema cache"
    store.append([make_row(message="registered 14 table mappings")])
    model.poll()
    assert model.heartbeat.message == "registered 14 table mappings"


def test_the_message_holds_still_while_nothing_arrives(store, make_row):
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(message="rendering 2.4M points at dpi=200")])
    for _ in range(10):
        model.poll()
    assert model.heartbeat.message == "rendering 2.4M points at dpi=200"


def test_a_multiline_message_is_cut_to_its_first_line(store, make_row):
    """A dumped payload would otherwise push the bars down the screen."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(message="query plan:\nSCAN records\nUSE TEMP B-TREE")])
    model.poll()
    assert model.heartbeat.message == "query plan:"


def test_a_task_event_is_not_echoed_as_the_last_line(store, make_row):
    """It has an exact named bar of its own, and it is not in `events`
    either — so echoing it would have the row narrate a record it says it
    never saw. The ordinary line behind it stands instead."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(
        [
            make_row(message="wrote batch 3"),
            make_row(
                message="task progress: reindex 40/100",
                task_id=1,
                task_event="update",
                task_label="reindex",
            ),
        ]
    )
    model.poll()
    assert model.heartbeat.message == "wrote batch 3"


def test_a_record_with_nothing_to_say_is_skipped(store, make_row):
    """`log.info("")` happens, and a blank column is not a message. The last
    line that actually said something stands."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(message="warming schema cache")])
    model.poll()
    store.append([make_row(message="   "), make_row(message="")])
    model.poll()
    assert model.heartbeat.message == "warming schema cache"


def test_a_run_of_records_worth_skipping_leaves_the_message_alone(store, make_row):
    """The lookback is short on purpose, and running off the end of it is
    not licence to invent: the previous line stands."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(message="fetched row 41")])
    model.poll()
    store.append(
        [
            make_row(message="something looked odd", level_name="WARNING", level_no=30)
            for _ in range(MESSAGE_LOOKBACK + 1)
        ]
    )
    model.poll()
    assert model.heartbeat.message == "fetched row 41"


def test_a_record_printed_above_the_bars_is_not_echoed(store, make_row):
    """A WARNING scrolls above the display in full, with its level and
    logger. Repeating it here says the same thing twice, and leaves a one-off
    warning sitting in the live row as if it were the current state."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(message="processing item 49")])
    model.poll()
    store.append(
        [make_row(message="something looked odd", level_name="WARNING", level_no=30)]
    )
    model.poll()
    assert model.heartbeat.events == 2, "a warning is still a record that arrived"
    assert model.heartbeat.message == "processing item 49"


def test_what_counts_as_printed_above_is_the_displays_call(store, make_row):
    """The renderer hands its own passthrough level over rather than the
    model guessing one: a display that collapses everything should echo
    everything."""
    model = RepeatingSourceModel(
        store,
        min_repeats=3,
        heartbeat=SessionHeartbeat(store, passthrough_level=logging.CRITICAL),
    )
    store.append(
        [make_row(message="something looked odd", level_name="WARNING", level_no=30)]
    )
    model.poll()
    assert model.heartbeat.message == "something looked odd"


def test_the_heartbeat_adds_no_second_store_poller(store, make_row, monkeypatch):
    """The watermark exists so a redraw costs what arrived rather than what
    the store holds. A second delta query per poll would double that to
    re-derive a number the first one already carries."""
    deltas = 0
    real = store.count_by_source_since

    def counting(after_id: int):
        nonlocal deltas
        deltas += 1
        return real(after_id)

    monkeypatch.setattr(store, "count_by_source_since", counting)
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 4))
    model.poll()
    assert model.heartbeat.events == 4
    assert deltas == 1


def test_a_quiet_poll_reads_nothing_at_all(store, make_row, monkeypatch):
    """Not just the delta: the message costs a `recent()` too, and a poll
    that brought no records cannot have a new last line."""
    model = RepeatingSourceModel(store, min_repeats=3)
    store.append(_loop_rows(make_row, 2))
    model.poll()

    reads = 0
    real = store.recent

    def counting(*args, **kwargs):
        nonlocal reads
        reads += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "recent", counting)
    for _ in range(5):
        model.poll()
    assert reads == 0


def test_the_heartbeat_survives_a_rich_free_install(store, make_row, monkeypatch):
    """Principle 9, and the bare-install CI leg in miniature: the model half
    of the display must import and run with `rich` unimportable. Re-imported
    rather than merely called, so a `from rich...` added at the top of one of
    those modules later fails here too."""
    import lumberjack.renderers as renderers

    name = "lumberjack.renderers.progress"
    # Every module in the package, not just its `__init__`. Dropping the
    # package alone would re-execute one file whose imports all hit the module
    # cache, so a `from rich...` added to `sources.py` would sail past this.
    submodules = [key for key in sys.modules if key.startswith(f"{name}.")]
    # Restored by monkeypatch afterwards: re-importing rebinds both the entry
    # in sys.modules and the attribute on the parent package.
    monkeypatch.setattr(renderers, "progress", renderers.progress)
    for module_name in [name, *submodules]:
        monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.setitem(sys.modules, "rich", None)  # `import rich` now raises

    module = importlib.import_module(name)
    model = module.RepeatingSourceModel(store, min_repeats=3)
    store.append([make_row(created=100.0), make_row(created=101.0)])
    model.poll()
    assert model.heartbeat.events == 2
    assert model.heartbeat.rate == pytest.approx(1.0)
    assert model.heartbeat.glyph
