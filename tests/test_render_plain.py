from __future__ import annotations

import io
import json
import re

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


def test_declares_write_through():
    # Teardown relies on this to decide whether replaying the tail at exit
    # would duplicate output that was already printed.
    assert PlainTextRenderer.write_through is True


def test_close_is_a_noop():
    renderer = PlainTextRenderer(stream=io.StringIO())
    renderer.close()
