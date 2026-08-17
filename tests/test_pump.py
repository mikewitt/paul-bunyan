"""Unit tests for the periodic buffer→store drain.

Timing-free by construction: the pump signals a threading.Event and the test
waits on it, so these neither sleep for a fixed duration nor race.
"""

from __future__ import annotations

import threading

import pytest

from lumberjack.pump import FlushPump


def test_non_positive_interval_is_rejected():
    with pytest.raises(ValueError):
        FlushPump(interval=0, flush=lambda: None)


def test_pump_calls_flush():
    called = threading.Event()
    pump = FlushPump(interval=0.001, flush=called.set)
    pump.start()
    try:
        assert called.wait(timeout=5.0)
    finally:
        pump.stop()


def test_pump_calls_flush_repeatedly():
    calls = 0
    enough = threading.Event()

    def flush() -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            enough.set()

    pump = FlushPump(interval=0.001, flush=flush)
    pump.start()
    try:
        assert enough.wait(timeout=5.0)
    finally:
        pump.stop()


def _pump_threads(name: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == name]


def test_start_is_idempotent():
    pump = FlushPump(interval=0.001, flush=lambda: None, name="idempotent-pump")
    pump.start()
    try:
        pump.start()
        assert len(_pump_threads("idempotent-pump")) == 1
    finally:
        pump.stop()


def test_stop_without_start_is_a_noop():
    FlushPump(interval=0.001, flush=lambda: None).stop()


def test_flush_exception_does_not_kill_the_pump():
    survived = threading.Event()
    calls = 0

    def flush() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("store write failed")
        survived.set()

    pump = FlushPump(interval=0.001, flush=flush)
    pump.start()
    try:
        assert survived.wait(timeout=5.0), "pump died on the first exception"
    finally:
        pump.stop()


def test_pump_thread_is_daemon():
    # A stalled pump must never wedge interpreter shutdown.
    pump = FlushPump(interval=0.001, flush=lambda: None, name="daemon-pump")
    pump.start()
    try:
        threads = _pump_threads("daemon-pump")
        assert threads and all(t.daemon for t in threads)
    finally:
        pump.stop()


def test_stopped_pump_leaves_no_thread_behind():
    pump = FlushPump(interval=0.001, flush=lambda: None, name="tidy-pump")
    pump.start()
    pump.stop()
    assert _pump_threads("tidy-pump") == []
