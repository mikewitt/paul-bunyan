from __future__ import annotations

import io

from lumberjack.detect import OutputMode, OutputModeDetector


class _FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_override_wins(monkeypatch):
    monkeypatch.delenv("LUMBERJACK_OUTPUT_MODE", raising=False)
    detector = OutputModeDetector(
        stream=_FakeStream(tty=True), override=OutputMode.JSON
    )
    assert detector.detect() is OutputMode.JSON


def test_override_accepts_string(monkeypatch):
    monkeypatch.delenv("LUMBERJACK_OUTPUT_MODE", raising=False)
    detector = OutputModeDetector(stream=_FakeStream(tty=False), override="plain")
    assert detector.detect() is OutputMode.PLAIN


def test_env_var_wins_over_tty(monkeypatch):
    monkeypatch.setenv("LUMBERJACK_OUTPUT_MODE", "json")
    detector = OutputModeDetector(stream=_FakeStream(tty=True))
    assert detector.detect() is OutputMode.JSON


def test_tty_without_rich_falls_back_to_plain(monkeypatch):
    monkeypatch.delenv("LUMBERJACK_OUTPUT_MODE", raising=False)
    monkeypatch.setattr("lumberjack.renderers.rich_available", lambda: False)
    detector = OutputModeDetector(stream=_FakeStream(tty=True))
    assert detector.detect() is OutputMode.PLAIN


def test_non_tty_defaults_to_plain(monkeypatch):
    monkeypatch.delenv("LUMBERJACK_OUTPUT_MODE", raising=False)
    detector = OutputModeDetector(stream=_FakeStream(tty=False))
    assert detector.detect() is OutputMode.PLAIN
