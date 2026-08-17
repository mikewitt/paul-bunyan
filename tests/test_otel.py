"""Outbound OTel: `task()` emitting spans.

Two halves. The degradation tests monkeypatch `otel.tracer()` to None, so the
absent path is exercised on *every* CI job rather than only the bare one; the
real-provider tests `importorskip` the SDK.

No test calls `set_tracer_provider()`. A second call in one process is
silently ignored — it logs "Overriding of current TracerProvider is not
allowed" through the `opentelemetry.trace` logger, not the `warnings` module,
so `filterwarnings = ["error"]` would not catch it and two tests would quietly
share the first exporter. Providers are constructed and injected through the
`otel.tracer()` seam instead, which is the second thing that seam buys.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pytest

import lumberjack
from lumberjack import otel

if TYPE_CHECKING:
    from opentelemetry.trace import Span

trace_sdk = pytest.importorskip("opentelemetry.sdk.trace")
export = pytest.importorskip("opentelemetry.sdk.trace.export")
in_memory = pytest.importorskip(
    "opentelemetry.sdk.trace.export.in_memory_span_exporter"
)


@pytest.fixture
def spans(monkeypatch) -> Iterator[list]:
    """A real SDK tracer wired to an in-memory exporter, injected through the
    `otel.tracer()` seam so no global provider is ever set."""
    exporter = in_memory.InMemorySpanExporter()
    provider = trace_sdk.TracerProvider()
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(otel, "tracer", lambda: tracer)
    yield exporter.get_finished_spans
    provider.shutdown()


def _named(get_spans) -> dict[str, object]:
    return {span.name: span for span in get_spans()}


# --- degradation: OTel absent ----------------------------------------------


def test_the_helpers_are_all_no_ops_without_otel(monkeypatch):
    """Each guard exercised directly, so a missing one cannot hide behind the
    others in a single end-to-end pass. `context_with_span(None)` is also
    exercised on its own here, so the parentless-task case is not only ever
    seen bundled with the rest.

    The task/track no-op behaviour itself — Principle 9: no OTel means
    no-ops, not errors — is pinned in test_tracking.py, which reaches the
    same absent-tracer path without needing a real OTel dependency at all."""
    monkeypatch.setattr(otel, "otel_trace", None)
    monkeypatch.setattr(otel, "otel_context", None)
    assert otel.tracer() is None
    assert otel.context_with_span(None) is None
    assert otel.attach(object()) is None
    assert otel.detach(None) is None
    assert otel.record_failure(cast("Span", object()), ValueError("x")) is None


# --- a real provider --------------------------------------------------------


def test_a_task_produces_a_span(spans):
    with lumberjack.task("reindex"):
        pass
    assert [s.name for s in spans()] == ["reindex"]


def test_the_span_carries_the_final_progress(spans):
    """Attributes are written once at end, never per tick."""
    with lumberjack.task("reindex", total=100) as t:
        for _ in range(5):
            t.advance()
    (span,) = spans()
    assert span.attributes["lumberjack.progress.current"] == 5
    assert span.attributes["lumberjack.progress.total"] == 100


def test_an_indeterminate_task_omits_the_total_attribute(spans):
    with lumberjack.task("scan") as t:
        t.advance()
    (span,) = spans()
    assert "lumberjack.progress.total" not in span.attributes


def test_nested_with_blocks_produce_a_parent_and_a_child(spans):
    with lumberjack.task("outer"):
        with lumberjack.task("inner"):
            pass
    by_name = _named(spans)
    assert by_name["inner"].parent.span_id == by_name["outer"].context.span_id


def test_subtask_parents_the_span_across_a_thread(spans):
    """The case that silently regresses if `.subtask()` reads ambient context:
    no contextvar reaches a bare thread, so the child span must be parented
    from `self._span` explicitly or the two hierarchies disagree."""
    with lumberjack.task("pool") as parent:

        def worker() -> None:
            with parent.subtask("worker"):
                pass

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    by_name = _named(spans)
    assert by_name["worker"].parent.span_id == by_name["pool"].context.span_id


def test_a_failing_task_marks_its_span(spans):
    with pytest.raises(ValueError):
        with lumberjack.task("doomed"):
            raise ValueError("boom")
    (span,) = spans()
    assert span.status.status_code.name == "ERROR"
    assert [e.name for e in span.events] == ["exception"]


def test_a_bare_handle_produces_a_span_without_becoming_a_parent(spans):
    """The span opens eagerly in `task()`, but only `__enter__` makes it
    current — so a handle never entered has a span and no children."""
    handle = lumberjack.task("never entered")
    with lumberjack.task("sibling"):
        pass
    handle.end()
    assert _named(spans)["sibling"].parent is None


def test_ending_twice_ends_the_span_once(spans):
    handle = lumberjack.task("once")
    handle.end()
    handle.end()
    assert len(spans()) == 1


def test_spans_are_produced_without_init(spans):
    """The two switches are independent: OTel configured but no `init()` means
    spans and no records."""
    assert not lumberjack.is_initialized()
    with lumberjack.task("standalone"):
        pass
    assert [s.name for s in spans()] == ["standalone"]


def test_track_produces_one_span_for_the_whole_iteration(spans):
    for _ in lumberjack.track(range(10), name="items"):
        pass
    (span,) = spans()
    assert span.name == "items"
    assert span.attributes["lumberjack.progress.current"] == 10


def test_a_level_the_logger_filters_out_still_produces_a_span(spans):
    """`isEnabledFor` gates the log record, never the span — the two switches
    are independent, so a quiet logger must not silence tracing."""
    logging.getLogger("lumberjack.task").setLevel(logging.CRITICAL)
    try:
        with lumberjack.task("quiet"):
            pass
    finally:
        logging.getLogger("lumberjack.task").setLevel(logging.NOTSET)
    assert [s.name for s in spans()] == ["quiet"]
