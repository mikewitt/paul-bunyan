from __future__ import annotations

import io

import pytest

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


# --- how the two overrides fail -------------------------------------------


@pytest.mark.parametrize("value", ["RICH", "rich ", " Rich"])
def test_env_var_is_case_and_whitespace_tolerant(monkeypatch, value):
    """The two things a human actually types when setting this by hand."""
    monkeypatch.setenv("LUMBERJACK_OUTPUT_MODE", value)
    monkeypatch.setattr("lumberjack.renderers.rich_available", lambda: True)
    assert OutputModeDetector(stream=_FakeStream(tty=False)).detect() is OutputMode.RICH


def test_unknown_env_var_warns_and_falls_back(monkeypatch):
    """A typo in an env var must not take the application down."""
    monkeypatch.setenv("LUMBERJACK_OUTPUT_MODE", "rihc")
    detector = OutputModeDetector(stream=_FakeStream(tty=True))
    with pytest.warns(RuntimeWarning, match="rihc"):
        assert detector.detect() is OutputMode.PLAIN


def test_unknown_explicit_override_still_raises(monkeypatch):
    """A bad constructor argument is a bug in the caller, not a typo."""
    monkeypatch.delenv("LUMBERJACK_OUTPUT_MODE", raising=False)
    with pytest.raises(ValueError):
        OutputModeDetector(override="rihc")
