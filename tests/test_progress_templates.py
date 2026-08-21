"""Tests for turning a message template into a row label (#56).

No `rich` here — describing a template is model work and runs on a bare
install like the rest of the models.
"""

from __future__ import annotations

import pytest

from lumberjack.renderers.progress import (
    LOOKBACKS,
    MAX_LABEL,
    describe_template,
)
from lumberjack.store import MIN_RETAIN


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("row %d: parsed", "row …: parsed"),
        ("batch %s of %s", "batch … of …"),
        ("%(name)s connected", "… connected"),
        ("%-8.3f seconds", "… seconds"),
        ("%*d items", "… items"),
        ("100%% done", "100% done"),
        ("no specifiers here", "no specifiers here"),
        ("first line\nsecond line", "first line"),
        ("  padded  ", "padded"),
        ("x" * 500, "x" * (MAX_LABEL - 1) + "…"),
    ],
)
def test_a_template_reads_as_a_description(template, expected):
    """The point of substituting rather than interpolating: the label says what
    the line does and stops changing. Interpolating the newest values would be
    a count wearing a description's clothes. A template longer than
    `MAX_LABEL` is truncated to exactly that length — a row's description
    shares one line with a bar, a count and a rate, and a log template can be
    a paragraph."""
    assert describe_template(template) == expected


def test_the_template_lookback_fits_inside_the_smallest_legal_store():
    """A definition kept in two places drifts in one of them.

    `TemplateIndex` harvests a row's label from a bounded tail read, so a
    retention bound below its largest lookback would make a slow
    announcement line's template unfindable — and the row would silently
    fall back to `file:line func()` while static grouping degraded with it.
    `MIN_RETAIN` is well clear of it today; this fails if either number
    moves toward the other.
    """
    assert max(LOOKBACKS) <= MIN_RETAIN
