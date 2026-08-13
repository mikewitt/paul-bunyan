"""The state a single `init()` owns, and the registry holding the live one.

`init()` and `teardown` need the same live components, and keeping them as
parallel module globals in both places meant two sets of names with nothing
keeping them in step — plus a `X is not None` guard at every use, because
each name was independently optional even though they are only ever set and
cleared together. One object instead: holding the `Session` means holding
all of it, and `current_session() is None` is the single question worth
asking of `init()`.

`teardown` keeps its own reference to the same object on purpose, as an
install token rather than a second copy of this state — it owns the
excepthook and the atexit hook on its own lifecycle, and its tests drive it
with fakes and no `init()` at all.

The registry lives here rather than in `__init__.py` so that modules
`__init__.py` imports can still ask whether lumberjack is running.
`tracking.py` needs exactly that.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

    from lumberjack.detect import OutputMode
    from lumberjack.handler import LumberjackHandler
    from lumberjack.pump import FlushPump
    from lumberjack.renderers import Renderer
    from lumberjack.store import RecordStore


@dataclasses.dataclass(slots=True)
class Session:
    """Everything one `init()` installed, and what it needs to put back."""

    handler: LumberjackHandler
    store: RecordStore
    renderer: Renderer
    output_mode: OutputMode
    #: True when lumberjack created the store, and so should close it.
    owns_store: bool
    #: How many records the atexit dump replays; 0 disables it.
    dump_last_n: int
    #: Root logger state as `init()` found it.
    prev_handlers: list[logging.Handler]
    prev_level: int
    #: Absent when `flush_interval=0` — the only genuinely optional member.
    pump: FlushPump | None = None


# The one piece of module state, and it is unguarded by any lock.
# lumberjack: see issue #11
_current: Session | None = None


def current_session() -> Session | None:
    """The session `init()` installed, or None if lumberjack is not running."""
    return _current


def set_current_session(session: Session | None) -> None:
    """Publish (or clear) the live session. Called by `init()`/`shutdown()`."""
    global _current
    _current = session
