"""In-process unit tests for install/uninstall/idempotency/hook-chaining.

The excepthook-fires-for-real and atexit-fires-for-real paths can't be
exercised in-process (pytest owns exception handling; atexit only runs at
real interpreter shutdown) — those are covered via subprocess in
test_integration.py instead.
"""

from __future__ import annotations

import sys

import pytest

from lumberjack import teardown


class _FakeRenderer:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeHandler:
    def __init__(self, rows: list[str] | None = None) -> None:
        self._rows = list(rows or [])
        self.drained = 0
        self.peeked: list[int | None] = []

    def drain(self) -> list[str]:
        self.drained += 1
        rows, self._rows = self._rows, []
        return rows

    def peek(self, n: int | None = None) -> list[str]:
        self.peeked.append(n)
        return list(self._rows)


class _FakeStore:
    def __init__(self) -> None:
        self.appended: list[str] = []

    def append(self, rows: list[str]) -> None:
        self.appended.extend(rows)


@pytest.fixture(autouse=True)
def _uninstall_after() -> None:
    yield
    teardown.uninstall()


def test_install_sets_excepthook():
    prev_hook = sys.excepthook
    teardown.install(
        renderer=_FakeRenderer(), handler=_FakeHandler(), store=_FakeStore()
    )
    assert sys.excepthook is teardown._excepthook
    teardown.uninstall()
    assert sys.excepthook is prev_hook


def test_install_twice_is_a_noop():
    renderer1, renderer2 = _FakeRenderer(), _FakeRenderer()
    handler, store = _FakeHandler(), _FakeStore()
    teardown.install(renderer=renderer1, handler=handler, store=store)
    teardown.install(renderer=renderer2, handler=handler, store=store)
    assert teardown._renderer is renderer1


def test_excepthook_closes_renderer_before_delegating(monkeypatch):
    renderer = _FakeRenderer()
    handler = _FakeHandler()
    store = _FakeStore()
    calls: list[str] = []
    teardown.install(renderer=renderer, handler=handler, store=store)
    monkeypatch.setattr(
        teardown, "_prev_excepthook", lambda *a: calls.append("prev_hook")
    )
    teardown._excepthook(RuntimeError, RuntimeError("x"), None)
    assert renderer.closed == 1
    assert calls == ["prev_hook"]


def test_teardown_flushes_buffer_to_store():
    renderer = _FakeRenderer()
    handler = _FakeHandler(rows=["a", "b"])
    store = _FakeStore()
    teardown.install(renderer=renderer, handler=handler, store=store)
    teardown._teardown()
    assert store.appended == ["a", "b"]
    assert handler.drained == 1


def test_teardown_is_idempotent():
    renderer = _FakeRenderer()
    handler = _FakeHandler(rows=["a"])
    store = _FakeStore()
    teardown.install(renderer=renderer, handler=handler, store=store)
    teardown._teardown()
    teardown._teardown()  # must not raise
    assert renderer.closed == 2


def test_teardown_diagnostics_dumped_before_drain():
    renderer = _FakeRenderer()
    handler = _FakeHandler(rows=["a", "b"])
    store = _FakeStore()
    teardown.install(renderer=renderer, handler=handler, store=store, dump_last_n=5)
    teardown._teardown()
    assert handler.peeked == [5]


def test_uninstall_restores_previous_hook():
    prev_hook = sys.excepthook
    teardown.install(
        renderer=_FakeRenderer(), handler=_FakeHandler(), store=_FakeStore()
    )
    teardown.uninstall()
    assert sys.excepthook is prev_hook


def test_uninstall_without_install_is_a_noop():
    teardown.uninstall()
