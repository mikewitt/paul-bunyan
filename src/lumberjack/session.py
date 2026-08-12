"""The state a single `init()` owns.

`init()` and `teardown` need the same live components, and keeping them as
parallel module globals in both places meant two sets of names with nothing
keeping them in step — plus a `X is not None` guard at every use, because
each name was independently optional even though they are only ever set and
cleared together. One object instead: holding the `Session` means holding
all of it, and `_session is None` is the single question worth asking.
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
