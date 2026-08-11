from __future__ import annotations

import logging

import pytest

import lumberjack


def test_init_replaces_root_handlers_by_default():
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    lumberjack.init(output_mode="plain")
    assert sentinel not in root.handlers


def test_init_can_layer_instead_of_replace():
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        lumberjack.init(output_mode="plain", replace_handlers=False)
        assert sentinel in root.handlers
    finally:
        root.removeHandler(sentinel)


def test_double_init_raises():
    lumberjack.init(output_mode="plain")
    with pytest.raises(RuntimeError):
        lumberjack.init(output_mode="plain")


def test_shutdown_restores_previous_handlers():
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        lumberjack.init(output_mode="plain")
        lumberjack.shutdown()
        assert sentinel in root.handlers
    finally:
        root.removeHandler(sentinel)


def test_init_without_rich_installed_still_works(monkeypatch):
    monkeypatch.setattr("lumberjack.renderers.rich_available", lambda: False)
    lumberjack.init(output_mode="rich")
    logger = logging.getLogger("no-rich-test")
    logger.info("still works")


def test_init_captures_log_records_into_buffer():
    handler = lumberjack.init(output_mode="plain")
    logger = logging.getLogger("capture-test")
    logger.info("captured")
    assert any(r.message == "captured" for r in handler.peek())


def test_shutdown_without_init_is_a_noop():
    lumberjack.shutdown()
