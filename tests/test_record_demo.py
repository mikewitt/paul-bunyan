"""The demo recorder decides what the README's image shows, and had no tests.

`scripts/record_demo.py` is not shipped and not imported by anything, so a
defect in it is invisible until someone looks at the published gif and says
it changed. That is how this file came to exist: the recorder's `--stop-at`
guard silently stopped working, the gif grew from 180px to 294px, and its
final frame became a wall of post-shutdown summary text instead of the
finished bars — with nothing red anywhere, because a gif that records
*something* passes every check the workflow makes.

The tests are on `_split_at`, which is the seam that decides when to stop.
Driving `capture()` itself would mean forking a pty and racing the same
timing that caused the bug, which is the one thing a regression test for it
must not do.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "record_demo.py"


@pytest.fixture(scope="module")
def recorder() -> Any:
    """`record_demo.py` loaded by path, as `test_benchmark.py` loads its own.

    No `importorskip` here, deliberately. `pyte` and `Pillow` are not
    dependencies of this project, and while the recorder imported them at
    module scope these tests could only skip — which for a regression test is
    the same as not having one. The recorder now imports them where it uses
    them, so the pure parts run everywhere, including the bare install.
    """
    spec = importlib.util.spec_from_file_location("record_demo", _SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{_SCRIPT} could not be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_demo"] = module
    spec.loader.exec_module(module)
    return module


#: The demo's summary header, and the recorder's default `--stop-at`.
MARKER = "=== "


def test_a_marker_arriving_alone_is_found(recorder) -> None:
    head, reached, _ = _split(recorder, "", f"bars\r\n{MARKER}pipeline ===\r\n")
    assert reached
    assert head == "bars\r\n", head


def test_nothing_after_the_marker_is_fed(recorder) -> None:
    """The frame kept is the display as it stood *before* the summary, so
    everything from the marker onward is dropped rather than rendered."""
    head, reached, _ = _split(recorder, "", f"live\r\n{MARKER}x ===\r\nrows\r\n")
    assert reached
    assert MARKER not in head
    assert "rows" not in head


def test_a_whole_summary_in_one_chunk_is_still_found(recorder) -> None:
    """The regression. ~30 lines of summary onto a 14-row screen in a single
    `os.read`: the old check fed the chunk and then asked whether the marker
    was *visible*, by which time it had scrolled off and the recording ran to
    process exit. Reading the bytes does not care how they were chunked."""
    chunk = f"{MARKER}pipeline ===\r\n" + "".join(
        f"  detail line {i}\r\n" for i in range(28)
    )
    _, reached, _ = _split(recorder, "", chunk)
    assert reached


def test_a_marker_split_across_two_reads_is_found(recorder) -> None:
    """`===` can land at the end of one read and ` ` at the start of the next.
    The carry is what makes that a hit rather than a miss."""
    _, reached, carry = _split(recorder, "", "bars\r\n==")
    assert not reached
    assert carry, "nothing carried, so the split marker cannot be rejoined"
    _, reached, _ = _split(recorder, carry, "= pipeline ===\r\n")
    assert reached


def test_an_empty_marker_keeps_everything(recorder) -> None:
    """`--stop-at ''` is the documented way to record the whole run, so an
    empty marker must never claim a hit and never hold anything back."""
    head, reached, carry = recorder._split_at("", "anything at all", "")
    assert not reached
    assert head == "anything at all"
    assert carry == ""


def test_a_miss_carries_only_the_tail_it_needs(recorder) -> None:
    """The carry is bounded by the marker's length, not by the output — this
    runs for the length of the recording, so it must not accumulate."""
    _, _, carry = _split(recorder, "", "x" * 10_000)
    assert len(carry) == len(MARKER) - 1, carry


def test_ordinary_output_is_passed_straight_through(recorder) -> None:
    head, reached, _ = _split(recorder, "", "fetched row 3 from source table\r\n")
    assert not reached
    assert head == "fetched row 3 from source table\r\n"


def _split(recorder: Any, carry: str, text: str) -> tuple[str, bool, str]:
    return recorder._split_at(carry, text, MARKER)
