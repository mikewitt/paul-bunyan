from __future__ import annotations

import io

import pytest

from lumberjack.detect import (
    MAX_BARS_ENV_VAR,
    OutputMode,
    OutputModeDetector,
    resolve_max_bars,
)

#: Tier 2 — a component contract, driven through a public component API.
#: See tests/README.md; `test_tier2_rules.py` checks what the mark claims.
pytestmark = pytest.mark.tier2


class _FakeStream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
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


# --- the opt-in bar ceiling ------------------------------------------------
#
# An environment variable rather than an init() option, and a terminal-compat
# aid rather than a feature: capping was never the answer to a high row count.
# See the note on MAX_BARS_ENV_VAR. Same policy as LUMBERJACK_OUTPUT_MODE
# above — a bad argument raises, a bad environment variable warns — which is
# why this lives here rather than beside the progress-model tests.


@pytest.mark.parametrize(
    ("env_value", "explicit", "expected"),
    [
        (None, None, None),  # no ceiling by default
        ("12", None, 12),  # the environment sets it
        ("12", 3, 3),  # an explicit override beats the environment
    ],
)
def test_ceiling_resolution_prefers_the_explicit_argument_then_the_environment(
    monkeypatch, env_value, explicit, expected
):
    if env_value is None:
        monkeypatch.delenv(MAX_BARS_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MAX_BARS_ENV_VAR, env_value)
    assert resolve_max_bars(explicit) == expected


@pytest.mark.parametrize("value", ["banana", "", "0", "-4", "3.5"])
def test_an_unusable_environment_value_warns_and_draws_everything(monkeypatch, value):
    """An operator typo must not cap at something surprising, or take the run
    down. Same split as LUMBERJACK_OUTPUT_MODE: env typos warn and degrade."""
    monkeypatch.setenv(MAX_BARS_ENV_VAR, value)
    if value == "":
        # Unset and empty are the same request: no ceiling, nothing to warn about.
        assert resolve_max_bars() is None
        return
    with pytest.warns(RuntimeWarning, match=MAX_BARS_ENV_VAR):
        assert resolve_max_bars() is None


@pytest.mark.parametrize("value", [0, -1])
def test_an_unusable_explicit_ceiling_raises(monkeypatch, value):
    """A bad argument is the caller's bug, so it raises rather than warns."""
    monkeypatch.delenv(MAX_BARS_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="must be positive"):
        resolve_max_bars(value)
