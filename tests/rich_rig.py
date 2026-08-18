"""The rich display test rig, shared by the two files that drive a live frame.

Not in `conftest.py`, and the reason is a required CI check rather than
taste: the `bare install (no rich)` job runs the whole suite with rich
absent, and conftest is imported unconditionally by every run. Anything
here touches `rich` at module scope, so importing it from conftest would
turn that job's skips into collection errors.

`tests/fixture_sources.py` is the existing precedent — a plain module the
files that need it import by name. Import this one only after
`pytest.importorskip("rich")`.

The `as_terminal` fixture is deliberately *not* here: a fixture body only
runs when a test asks for it, so conftest can hold it with the rich import
inside the function. Importing a fixture into a test module instead makes
every test that names it as a parameter look like a redefinition (F811).

What is *not* shared is the rig factory. `test_render_progress.py` builds
its pipeline on a named logger and `test_render_tasks.py` on the root
logger with a published `Session`, which is the difference the two files
exist to cover; only the parts that were byte-identical live here.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import re

from lumberjack.handler import LumberjackHandler
from lumberjack.renderers.rich_renderer import RichProgressRenderer
from lumberjack.store import RecordStore


@dataclasses.dataclass
class Rig:
    """A whole lumberjack pipeline, with the timers replaced by `tick()`."""

    logger: logging.Logger
    handler: LumberjackHandler
    store: RecordStore
    renderer: RichProgressRenderer
    stream: io.StringIO

    def tick(self) -> None:
        """One flush-pump drain plus one redraw, run synchronously."""
        self.store.append(self.handler.drain())
        self.renderer.refresh()

    def output(self) -> str:
        return self.stream.getvalue()


def line(frame: str, needle: str) -> str:
    """The needle's line as the *last* frame drew it.

    A live display rewrites in place, so the captured stream holds every frame
    since the first, separated by carriage returns as well as newlines. The
    interesting one is always the most recent.
    """
    return next(ln for ln in reversed(re.split(r"[\r\n]", frame)) if needle in ln)
