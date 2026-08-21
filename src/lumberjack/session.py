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
import threading
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
    #: How many records the store keeps before the oldest are evicted; None
    #: is unbounded. Validated by `init()`, enforced by `flush()`.
    retain: int | None = None
    #: Rows written to the store by this session, exactly.
    #:
    #: Exact rather than estimated because `LumberjackHandler` is the only
    #: writer and `evict()` returns the number of rows it deleted — so the
    #: count is maintained by arithmetic on both sides and never needs a
    #: `COUNT(*)`, which at a million rows is the kind of query a drain must
    #: not make five times a second.
    #:
    #: A caller-supplied store may already hold rows this has never seen, so
    #: it is what *this session* wrote rather than what the store contains.
    #: Retention overshooting on the first trim of a pre-populated store is
    #: the cost, and it self-corrects on the next one.
    stored: int = 0
    #: Absent when `flush_interval=0` — the only genuinely optional member.
    pump: FlushPump | None = None


_current: Session | None = None

#: Serializes installing and tearing down a session against the callers that
#: read one and then use what they read.
#:
#: `flush()` is the case that forced it: it takes the session, then writes to
#: that session's store. A concurrent `shutdown()` closing the store between
#: those two steps raises `sqlite3.ProgrammingError` — not into lumberjack,
#: but into whichever worker thread happened to call `flush()`.
#:
#: Reentrant because `shutdown()` calls `flush()` while holding it.
#:
#: Deliberately *not* taken by `current_session()`. That is read once per task
#: event by `tracking.py`, and locking it would serialize every instrumented
#: call in the process against a background flush. Publishing and reading a
#: single reference is atomic under CPython either way; what needs guarding is
#: the compound read-then-use, which is the caller's business and is where the
#: lock is applied.
_lock = threading.RLock()


def registry_lock() -> threading.RLock:
    """The lock guarding session install and teardown — see `_lock`."""
    return _lock


def current_session() -> Session | None:
    """The session `init()` installed, or None if lumberjack is not running.

    Unlocked, and therefore a snapshot: by the time a caller acts on it,
    `shutdown()` may have run. Anything that reads the session and then
    *uses* what it read must hold `registry_lock()` across both.
    """
    return _current


def set_current_session(session: Session | None) -> None:
    """Publish (or clear) the live session. Called by `init()`/`shutdown()`."""
    global _current
    _current = session
