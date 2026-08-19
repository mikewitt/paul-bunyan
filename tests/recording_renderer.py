"""A renderer that keeps what should have been drawn, and draws nothing.

The stand-in for `RichProgressRenderer` in a test that cares what the display
was *told* to do rather than what it looked like. It implements the `Renderer`
Protocol, so it can go anywhere the real one goes, and it needs no `rich`.

**It reimplements nothing, and that is the entire design.** It owns the same
`LoopRowModel` and `TaskProgressModel`, and its `refresh()` calls the same
`plan_frame()` the rich renderer calls. Every decision it records was made by
the code that runs in production; all this class adds is a list to keep the
answers in. A recorder that computed its own idea of what should be on screen
would be a second implementation, free to agree with itself while disagreeing
with the display — which is the failure a fixture like this exists to avoid,
not to introduce.

What it therefore cannot witness is exactly what `plan.py`'s docstring lists:
total withdrawal, re-layout, the elapsed clock, the one-`Live` rule, glyph
choice and cropping are rich's, and `tests/test_display_parity.py` plus the
rich-gated frame assertions in `test_render_progress.py` are what cover them.
Nothing here is a substitute for either, and no test should move out of them
onto this.

**"How many bars" is `counts()`, and it is seven numbers.** `sources` counts
call sites — the identity layer, what the store would corroborate — and
`loops` counts inferred loops, which is what a reader sees. They disagree by
design and by a lot: `siblings` is 400 iterations behind 1,600 records across
4 sources, rendered as one row.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from lumberjack.renderers.plan import plan_frame
from lumberjack.renderers.progress import (
    DEFAULT_MIN_REPEATS,
    HEARTBEAT_FRAMES,
    PASSTHROUGH_LEVEL,
    LoopRowModel,
    SessionHeartbeat,
    TaskProgressModel,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from lumberjack.renderers.plan import Frame, FrameCounts
    from lumberjack.schema import LogRecordRow
    from lumberjack.store import RecordStore


class RecordingRenderer:
    """Records one `Frame` per `refresh()`. Never writes to a stream.

    `refresh()` is driven by the test rather than by a timer: there is no
    pump here, because a fixture that redrew on its own would make every
    assertion a race. That is also why `render()` does nothing but count —
    the real renderer's `render()` only passes high-level records through to
    the console, which is display, and the counting is what a test wants to
    assert against the frame.
    """

    #: A live display is lossy, and this one is lossier still. Declaring False
    #: is what tells teardown's exit dump to replay the tail.
    write_through = False

    def __init__(
        self,
        store: RecordStore,
        *,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        passthrough_level: int = PASSTHROUGH_LEVEL,
        max_bars: int | None = None,
        clock: Callable[[], float] | None = None,
        frames: str = HEARTBEAT_FRAMES,
        separator: str = "·",
    ) -> None:
        extra = {} if clock is None else {"clock": clock}
        self._model = LoopRowModel(
            store,
            min_repeats=min_repeats,
            heartbeat=SessionHeartbeat(store, passthrough_level=passthrough_level),
            **extra,
        )
        self._task_model = TaskProgressModel(store)
        self._max_bars = max_bars
        # Passed in rather than resolved, because there is no console here to
        # ask. Which glyphs a terminal can encode is rich's to decide — see
        # `plan.py`'s docstring — so a recorder that guessed would disagree
        # with the real display on any console that cannot take braille, and
        # would be *right* to. The defaults are what a utf-8 console gets.
        self._glyphs = frames
        self._separator = separator
        self._frames: list[Frame] = []
        self._rendered: list[LogRecordRow] = []
        # `render()` is called from whichever thread logged, concurrently, so
        # the list it appends to needs a lock even though nothing else here
        # runs off the test's thread.
        self._lock = threading.Lock()
        self._closed = False

    def render(self, row: LogRecordRow) -> None:
        """Count the record. After `close()` this is a no-op, not an error.

        The handler is still installed when `close()` runs — `shutdown()`
        closes the display before removing it — so a thread logging in that
        window still arrives here, and raising would surface as an exception
        from an ordinary `logging` call.
        """
        with self._lock:
            if self._closed:
                return
            self._rendered.append(row)

    def refresh(self) -> None:
        """Plan a frame and keep it. A no-op once closed."""
        if self._closed:
            return
        self._frames.append(
            plan_frame(
                rows=self._model.poll(),
                tasks=self._task_model.poll(),
                heartbeat=self._model.heartbeat,
                frames=self._glyphs,
                separator=self._separator,
                max_bars=self._max_bars,
            )
        )

    def close(self) -> None:
        """Idempotent, and does not release the store it was given."""
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def frame(self) -> Frame | None:
        """The last frame planned, or None before the first `refresh()`."""
        return self._frames[-1] if self._frames else None

    @property
    def frames(self) -> tuple[Frame, ...]:
        """Every frame, in order — so a test can assert on how a row *moved*
        rather than only on where it ended up."""
        return tuple(self._frames)

    @property
    def rendered(self) -> tuple[LogRecordRow, ...]:
        """The records that reached `render()`, which is not the same as the
        records that reached the store: this is the write-through path, and a
        lossy renderer sees only what the handler hands it live."""
        return tuple(self._rendered)

    def counts(self) -> FrameCounts:
        """How many bars the last frame said there should be."""
        frame = self.frame
        if frame is None:
            self.refresh()
            frame = self.frame
            assert frame is not None
        return frame.counts
