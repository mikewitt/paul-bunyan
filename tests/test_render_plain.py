from __future__ import annotations

import datetime
import io
import json
import re

import pytest

from lumberjack.renderers.plain import PlainTextRenderer

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


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
