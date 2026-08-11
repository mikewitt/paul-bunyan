from __future__ import annotations

import io
import json
import re
import time

from lumberjack.renderers.plain import PlainTextRenderer
from lumberjack.schema import LogRecordRow

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _row(**overrides: object) -> LogRecordRow:
    fields: dict[str, object] = dict(
        logger_name="test",
        level_name="INFO",
        level_no=20,
        msg="msg",
        message="hello world",
        pathname="/tmp/foo.py",
        filename="foo.py",
        module="foo",
        func_name="bar",
        lineno=10,
        created=time.time(),
        thread=1,
        thread_name="MainThread",
        process=100,
        process_name="MainProcess",
        exc_text=None,
        stack_text=None,
        task_name=None,
        task_id=None,
        parent_task_id=None,
        template_id=None,
    )
    fields.update(overrides)
    return LogRecordRow(**fields)  # type: ignore[arg-type]


def test_text_mode_contains_message_and_no_ansi():
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream)
    renderer.render(_row())
    output = stream.getvalue()
    assert "hello world" in output
    assert "INFO" in output
    assert not _ANSI_RE.search(output)


def test_json_lines_mode_is_valid_json_per_line():
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream, json_lines=True)
    renderer.render(_row(message="one"))
    renderer.render(_row(message="two"))
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["message"] == "one"
    assert parsed[1]["message"] == "two"
    assert not any(_ANSI_RE.search(line) for line in lines)


def test_render_writes_through_immediately():
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream)
    renderer.render(_row())
    assert stream.getvalue() != ""


def test_render_includes_exception_text():
    stream = io.StringIO()
    renderer = PlainTextRenderer(stream=stream)
    renderer.render(_row(exc_text="Traceback...\nValueError: x"))
    assert "ValueError: x" in stream.getvalue()


def test_close_is_a_noop():
    renderer = PlainTextRenderer(stream=io.StringIO())
    renderer.close()
