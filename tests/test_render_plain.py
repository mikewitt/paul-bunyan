from __future__ import annotations

import datetime
import io
import json
import re

import pytest

from lumberjack.renderers.plain import PlainTextRenderer

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


#: Tier 2 — a component contract, driven through a public component API.
#: See tests/README.md; `test_tier2_rules.py` checks what the mark claims.
pytestmark = pytest.mark.tier2


def test_text_mode_contains_message_and_no_ansi(make_row):
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream)
    renderer.render(make_row())
    output = stream.getvalue()
    assert "hello world" in output
    assert "INFO" in output
    assert not _ANSI_RE.search(output)


def test_json_lines_mode_is_valid_json_per_line(make_row):
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream, json_lines=True)
    renderer.render(make_row(message="one"))
    renderer.render(make_row(message="two"))
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["message"] == "one"
    assert parsed[1]["message"] == "two"
    assert not any(_ANSI_RE.search(line) for line in lines)


def test_json_lines_mode_carries_the_progress_columns(make_row):
    """`asdict()` picks up new fields for free, so this guards the guarantee
    rather than the mechanism: a JSON consumer sees progress without a join."""
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream, json_lines=True)
    renderer.render(
        make_row(
            task_label="reindex",
            task_event="update",
            task_id=7,
            progress_current=40,
            progress_total=100,
        )
    )
    parsed = json.loads(stream.getvalue())
    assert parsed["task_label"] == "reindex"
    assert parsed["task_event"] == "update"
    assert (parsed["progress_current"], parsed["progress_total"]) == (40, 100)


def test_render_writes_through_immediately(make_row):
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream)
    renderer.render(make_row())
    assert stream.getvalue() != ""


def test_render_includes_exception_text(make_row):
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream)
    renderer.render(make_row(exc_text="Traceback...\nValueError: x"))
    assert "ValueError: x" in stream.getvalue()


def test_close_is_a_noop():
    renderer = PlainTextRenderer(stream=io.StringIO())
    renderer.close()


# --- timestamps are unambiguous (#14) ---------------------------------------


def test_the_text_timestamp_carries_a_utc_offset(make_row):
    """This renderer is the safe choice for files and pipes, so its output is
    what gets shipped elsewhere and read later. A bare local timestamp cannot
    be ordered against one from another machine, or against itself across a
    DST boundary."""
    stream = io.StringIO()
    PlainTextRenderer(stream=stream).render(make_row(created=1786800000.0))
    stamp = stream.getvalue().split()[0]
    parsed = datetime.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"naive timestamp: {stamp}"
    assert parsed.timestamp() == pytest.approx(1786800000.0)


def test_the_text_timestamp_keeps_milliseconds(make_row):
    stream = io.StringIO()
    PlainTextRenderer(stream=stream).render(make_row(created=1786800000.123))
    stamp = stream.getvalue().split()[0]
    assert ".123" in stamp
    assert datetime.datetime.fromisoformat(stamp).timestamp() == pytest.approx(
        1786800000.123
    )


def test_json_lines_carry_both_the_epoch_and_an_iso_string(make_row):
    """A machine consumer wants to compare and bucket without parsing; a
    person reading the file wants to know when. Neither should have to
    convert, so both are present."""
    stream = io.StringIO()
    PlainTextRenderer(stream=stream, json_lines=True).render(
        make_row(created=1786800000.5)
    )
    payload = json.loads(stream.getvalue())
    assert payload["created"] == 1786800000.5
    parsed = datetime.datetime.fromisoformat(payload["timestamp"])
    assert parsed.tzinfo is not None
    assert parsed.timestamp() == pytest.approx(1786800000.5)


def test_both_modes_describe_the_same_instant(make_row):
    """One renderer, two output shapes — they must not disagree about when
    something happened."""
    text, structured = io.StringIO(), io.StringIO()
    row = make_row(created=1786800000.25)
    PlainTextRenderer(stream=text).render(row)
    PlainTextRenderer(stream=structured, json_lines=True).render(row)
    assert text.getvalue().split()[0] == json.loads(structured.getvalue())["timestamp"]


# --- a broken timestamp costs its column, not the line (#97) ---------------
#
# `created` is not always `time.time()`. `logging.makeLogRecord({...})`
# trusts a dict verbatim, a `logging.Filter` may rewrite it, and a library
# building a `LogRecord` by hand can get the units wrong — nanoseconds where
# seconds were meant puts an ordinary 2026 timestamp at ~1.76e18. Each of the
# values below raises a *different* exception out of
# `datetime.fromtimestamp`, and none of them is documented as the one to
# expect.

_UNRENDERABLE = pytest.mark.parametrize(
    "created",
    [
        pytest.param(1e18, id="too-large"),
        pytest.param(-1e18, id="too-negative"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("nan"), id="nan"),
    ],
)


@_UNRENDERABLE
def test_text_mode_still_writes_a_line_for_an_unrenderable_timestamp(
    created: float, make_row
):
    """The level, the logger and the message are the part that is not broken.

    `_isoformat` is the first thing `render()` does, so before this the raise
    took the whole line with it — and did so silently, since `emit()` routes
    the failure to `handleError()`.
    """
    stream = io.StringIO()
    PlainTextRenderer(stream=stream).render(
        make_row(created=created, message="the payload survived")
    )
    line = stream.getvalue()
    assert "the payload survived" in line
    assert "INFO" in line


@_UNRENDERABLE
def test_json_mode_still_writes_a_line_for_an_unrenderable_timestamp(
    created: float, make_row
):
    stream = io.StringIO()
    PlainTextRenderer(stream=stream, json_lines=True).render(
        make_row(created=created, message="the payload survived")
    )
    payload = json.loads(stream.getvalue())
    assert payload["message"] == "the payload survived"
    assert payload["level_name"] == "INFO"


def _strictly(line: str) -> dict[str, object]:
    """`json.loads`, minus Python's tolerance for literals JSON has no word for.

    The stdlib parser reads bare `NaN` and `Infinity` straight back, so a
    plain `json.loads` passes on a line that `jq`, Go and `JSON.parse` all
    reject — RFC 8259 has no such literals. `parse_constant` is the hook that
    makes it strict, and `created` is a raw float for machine consumers,
    which is exactly who cannot parse those.
    """

    def _reject(constant: str) -> float:
        raise AssertionError(f"not valid JSON: bare {constant}")

    parsed: dict[str, object] = json.loads(line, parse_constant=_reject)
    return parsed


@_UNRENDERABLE
def test_the_json_line_stays_valid_json_for_an_unrenderable_timestamp(
    created: float, make_row
):
    stream = io.StringIO()
    PlainTextRenderer(stream=stream, json_lines=True).render(
        make_row(created=created, message="the payload survived")
    )
    assert _strictly(stream.getvalue())["message"] == "the payload survived"


@pytest.mark.parametrize("created", [float("inf"), float("-inf"), float("nan")])
def test_a_timestamp_json_cannot_spell_becomes_null(created: float, make_row):
    """`null`, not a number, and not the line's absence.

    There is genuinely no timestamp to report, which is what `null` means;
    the value itself is still readable in the `timestamp` field beside it.
    Only the non-finite values need this — being out of `time_t` range is
    unrelated to being unspellable in JSON.
    """
    stream = io.StringIO()
    PlainTextRenderer(stream=stream, json_lines=True).render(make_row(created=created))
    payload = _strictly(stream.getvalue())
    assert payload["created"] is None
    assert payload["timestamp"] == repr(created)


@pytest.mark.parametrize("created", [1e18, -1e18, 1.76e18])
def test_a_finite_created_is_left_as_a_number_in_json(created: float, make_row):
    """The guard is for the values JSON cannot spell, not for every value the
    clock cannot render. 1e18 is out of `time_t` range and a perfectly
    ordinary JSON number, and blanking it would lose evidence for nothing."""
    stream = io.StringIO()
    PlainTextRenderer(stream=stream, json_lines=True).render(make_row(created=created))
    assert _strictly(stream.getvalue())["created"] == created


def test_the_fallback_timestamp_says_what_the_value_was(make_row):
    """A sentinel would say "broken"; the number says *how*.

    1.76e18 is an ordinary 2026 timestamp written in nanoseconds, and that is
    a diagnosis rather than a complaint.
    """
    stream = io.StringIO()
    PlainTextRenderer(stream=stream).render(make_row(created=1.76e18))
    assert "1.76e+18" in stream.getvalue()


def test_a_renderable_timestamp_is_untouched_by_the_guard(make_row):
    """The guard must not change the ordinary path — the one every record
    takes."""
    stream = io.StringIO()
    PlainTextRenderer(stream=stream).render(make_row(created=1_700_000_000.5))
    stamp = stream.getvalue().split()[0]
    assert datetime.datetime.fromisoformat(stamp).timestamp() == 1_700_000_000.5
