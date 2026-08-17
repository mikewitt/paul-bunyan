from __future__ import annotations

import io

import pytest

pytest.importorskip("rich")

import lumberjack.renderers.rich_renderer as rich_renderer_module
from lumberjack.renderers.rich_renderer import RichTerminalRenderer


def test_render_writes_message_to_stream(make_row):
    stream = io.StringIO()
    renderer = RichTerminalRenderer(stream=stream)
    renderer.render(make_row(level_name="ERROR", level_no=40))
    assert "hello world" in stream.getvalue()


def test_render_includes_exception_text(make_row):
    stream = io.StringIO()
    renderer = RichTerminalRenderer(stream=stream)
    renderer.render(
        make_row(level_name="ERROR", level_no=40, exc_text="RuntimeError: boom")
    )
    assert "RuntimeError: boom" in stream.getvalue()


def test_close_suppresses_further_renders(make_row):
    stream = io.StringIO()
    renderer = RichTerminalRenderer(stream=stream)
    renderer.close()
    renderer.render(make_row())
    assert stream.getvalue() == ""


def test_raises_clear_error_without_rich(monkeypatch):
    monkeypatch.setattr(rich_renderer_module, "Console", None)
    with pytest.raises(RuntimeError):
        rich_renderer_module.RichTerminalRenderer(stream=io.StringIO())
