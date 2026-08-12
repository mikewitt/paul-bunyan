"""Tests for the renderer factory and the optional-dependency degradation path.

Design principle 9: every optional dependency degrades, never errors. `rich`
missing must fall back to the plain renderer rather than raising, so that case
gets a real test — simulated by poisoning sys.modules rather than by
monkeypatching `rich_available` away, so the actual function body runs.
"""

from __future__ import annotations

import io
import sys

import pytest

from lumberjack.detect import OutputMode
from lumberjack.renderers import Renderer, create_renderer, rich_available
from lumberjack.renderers.plain import PlainTextRenderer


@pytest.fixture
def without_rich(monkeypatch) -> None:
    """Make `import rich` raise ImportError without uninstalling anything.

    A None entry in sys.modules is the documented way to force an import to
    fail; monkeypatch restores the real entry afterward.
    """
    monkeypatch.setitem(sys.modules, "rich", None)


def test_rich_available_true_when_installed():
    pytest.importorskip("rich")
    assert rich_available() is True


def test_rich_available_false_when_import_fails(without_rich):
    assert rich_available() is False


def test_plain_mode_creates_plain_renderer():
    renderer = create_renderer(OutputMode.PLAIN, stream=io.StringIO())
    assert isinstance(renderer, PlainTextRenderer)
    assert renderer.json_lines is False


def test_json_mode_creates_plain_renderer_in_json_lines_mode():
    renderer = create_renderer(OutputMode.JSON, stream=io.StringIO())
    assert isinstance(renderer, PlainTextRenderer)
    assert renderer.json_lines is True


def test_rich_mode_creates_rich_renderer_when_available():
    pytest.importorskip("rich")
    from lumberjack.renderers.rich_renderer import RichTerminalRenderer

    renderer = create_renderer(OutputMode.RICH, stream=io.StringIO())
    assert isinstance(renderer, RichTerminalRenderer)


def test_rich_mode_falls_back_to_plain_without_rich(without_rich):
    renderer = create_renderer(OutputMode.RICH, stream=io.StringIO())
    assert isinstance(renderer, PlainTextRenderer)


def test_rich_mode_with_a_store_creates_the_live_bar(store):
    pytest.importorskip("rich")
    from lumberjack.renderers.rich_renderer import RichProgressRenderer

    renderer = create_renderer(OutputMode.RICH, stream=io.StringIO(), store=store)
    try:
        assert isinstance(renderer, RichProgressRenderer)
    finally:
        renderer.close()


def test_rich_mode_without_a_store_stays_on_the_scrolling_renderer():
    # The bar reads its counts out of a store; with nowhere to read from,
    # there is nothing to draw.
    pytest.importorskip("rich")
    from lumberjack.renderers.rich_renderer import RichTerminalRenderer

    renderer = create_renderer(OutputMode.RICH, stream=io.StringIO())
    assert isinstance(renderer, RichTerminalRenderer)


def test_live_bar_falls_back_to_plain_without_rich(without_rich, store):
    renderer = create_renderer(OutputMode.RICH, stream=io.StringIO(), store=store)
    assert isinstance(renderer, PlainTextRenderer)


@pytest.mark.parametrize("mode", [OutputMode.PLAIN, OutputMode.JSON])
def test_a_store_never_buys_a_live_bar_off_a_tty(mode, store):
    # Never assume a human is watching: a bar redrawing itself into a pipe or
    # a log file is noise, however much there is to show.
    renderer = create_renderer(mode, stream=io.StringIO(), store=store)
    assert isinstance(renderer, PlainTextRenderer)
    assert renderer.write_through is True


def test_every_renderer_satisfies_the_protocol(make_row):
    # Includes `write_through`, which teardown reads off the renderer to decide
    # whether replaying the tail at exit would duplicate output.
    for mode in OutputMode:
        renderer = create_renderer(mode, stream=io.StringIO())
        assert isinstance(renderer, Renderer)
        assert isinstance(renderer.write_through, bool)
        renderer.render(make_row())
        renderer.close()
