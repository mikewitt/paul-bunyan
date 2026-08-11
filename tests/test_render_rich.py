from __future__ import annotations

import io
import time

import pytest

pytest.importorskip("rich")

import lumberjack.renderers.rich_renderer as rich_renderer_module
from lumberjack.renderers.rich_renderer import RichTerminalRenderer
from lumberjack.schema import LogRecordRow


def _row(**overrides: object) -> LogRecordRow:
    fields: dict[str, object] = dict(
        logger_name="test",
        level_name="ERROR",
        level_no=40,
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


def test_render_writes_message_to_stream():
    stream = io.StringIO()
    renderer = RichTerminalRenderer(stream=stream)
    renderer.render(_row())
    assert "hello world" in stream.getvalue()


def test_close_suppresses_further_renders():
    stream = io.StringIO()
    renderer = RichTerminalRenderer(stream=stream)
    renderer.close()
    renderer.render(_row())
    assert stream.getvalue() == ""


def test_raises_clear_error_without_rich(monkeypatch):
    monkeypatch.setattr(rich_renderer_module, "Console", None)
    with pytest.raises(RuntimeError):
        rich_renderer_module.RichTerminalRenderer(stream=io.StringIO())
