"""Thread safety around `session.py`'s registry and the lock guarding it (#11).

`current_session()` is the single "is lumberjack running" question, and
`flush()`/`shutdown()` both read it before acting on what they find. These
tests force the two reachable races rather than hoping to catch them: a
`flush()` in flight when `shutdown()` closes the store out from under it, and
two `shutdown()` calls racing each other for who tears the session down.
"""

from __future__ import annotations

import logging
import threading

import lumberjack
from lumberjack import session as _registry


def test_flush_during_shutdown_does_not_hit_a_closed_store():
    """The reachable race, and the reason this needs a lock at all.

    A worker calling `flush()` reads the session, then writes to that
    session's store. `shutdown()` closing the store in between raises
    `sqlite3.ProgrammingError` — not inside lumberjack, but out of the
    worker's own `flush()` call. This package is aimed squarely at concurrent
    programs, so that is not an exotic interleaving.

    Forced rather than raced: the store's `append` blocks until released, so
    the worker is provably inside the critical section when `shutdown()`
    starts. A timer releases it, because the main thread is by then blocked on
    the lock and cannot release anything itself.
    """
    lumberjack.init(output_mode="plain", flush_interval=0)
    store = lumberjack.current_store()
    assert store is not None

    inside_append = threading.Event()
    release = threading.Event()
    real_append = store.append

    def blocking_append(rows):
        inside_append.set()
        release.wait(5)
        real_append(rows)

    store.append = blocking_append
    logging.getLogger().warning("something to flush")

    escaped: list[BaseException] = []

    def worker() -> None:
        try:
            lumberjack.flush()
        except BaseException as exc:  # noqa: BLE001 - recording it is the test
            escaped.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert inside_append.wait(5), "worker never reached the store write"

    threading.Timer(0.2, release.set).start()
    lumberjack.shutdown()
    thread.join(5)

    assert not escaped, f"flush() raised into the caller: {escaped}"


def test_a_second_shutdown_racing_the_first_is_a_no_op():
    """`shutdown()` stops the pump *outside* the lock — it has to, since
    joining the pump thread from inside deadlocks against `flush()`. That
    leaves a window where another thread can complete the whole teardown, so
    the session is re-read once the lock is held.

    Forced rather than raced: the first caller is parked inside a stub
    `pump.stop()` until the second has finished, which is exactly the
    interleaving the re-read exists for.
    """
    lumberjack.init(output_mode="plain", flush_interval=0)
    session = _registry.current_session()
    assert session is not None

    stopping = threading.Event()
    proceed = threading.Event()

    class _ParkedPump:
        def stop(self) -> None:
            stopping.set()
            proceed.wait(5)

    session.pump = _ParkedPump()

    escaped: list[BaseException] = []

    def first_caller() -> None:
        try:
            lumberjack.shutdown()
        except BaseException as exc:  # noqa: BLE001 - recording it is the test
            escaped.append(exc)

    thread = threading.Thread(target=first_caller)
    thread.start()
    assert stopping.wait(5), "the first caller never reached pump.stop()"

    # Let the second caller past the same gate, then let it finish first.
    session.pump = None
    lumberjack.shutdown()
    assert not lumberjack.is_initialized()

    proceed.set()
    thread.join(5)
    assert not escaped, f"the losing shutdown() raised: {escaped}"
    assert not lumberjack.is_initialized()
