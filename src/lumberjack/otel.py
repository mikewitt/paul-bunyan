"""Outbound OpenTelemetry: `task()` emitting spans.

The only module allowed to import `opentelemetry`, and it does so guarded, so
importing `lumberjack` never requires it (Principle 9). Absent, `tracer()`
returns None and every span call in `tracking.py` becomes a no-op.

Spans depend on **OTel's** configuration, never on `lumberjack.init()`. An
application that has not set a tracer provider gets OTel's own `NoOpTracer`,
which costs nothing and exports nothing — so lumberjack adds no gating of its
own beyond the import guard. Library code never calls `set_tracer_provider()`;
that is the application's to own, and a second call is silently ignored.

`tracer()` is a function rather than a module-level constant on purpose. One
monkeypatched seam then forces the absent path on every CI job rather than
only the bare one, and the OTel tests can inject a provider of their own
without touching global state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    _OTEL_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only without otel
    otel_context = None  # type: ignore[assignment]
    otel_trace = None  # type: ignore[assignment]
    _OTEL_IMPORT_ERROR = exc

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.trace import Span, Tracer

#: Instrumentation scope every lumberjack span is reported under.
INSTRUMENTATION_NAME = "lumberjack"


def tracer() -> Tracer | None:
    """The tracer to open spans on, or None when OTel is not installed.

    Resolved per call rather than cached: `opentelemetry.trace.get_tracer`
    hands back a `ProxyTracer` that binds to the real provider lazily, so an
    application is free to configure OTel after importing lumberjack.
    """
    if otel_trace is None:
        return None
    return otel_trace.get_tracer(INSTRUMENTATION_NAME)


def context_with_span(span: Span | None) -> Context | None:
    """A `Context` in which `span` is current, for parenting a child span.

    Built explicitly from the parent span rather than read from ambient
    context, so a child opened on a worker thread — where no contextvar
    propagated — still lands under its parent. Without this the log hierarchy
    and the span hierarchy disagree in exactly that case.
    """
    if otel_trace is None or span is None:
        return None
    return otel_trace.set_span_in_context(span)


def attach(span: Span) -> object | None:
    """Make `span` current, returning a token for `detach()`.

    Unconditional when OTel imported — deliberately *not* gated on
    `span.is_recording()`, which is also False for a span the provider sampled
    out. Skipping the attach for those would break parent/child propagation
    under head sampling, silently and only in production.
    """
    if otel_context is None or otel_trace is None:
        return None
    return otel_context.attach(otel_trace.set_span_in_context(span))


def detach(token: object | None) -> None:
    """Undo the `attach()` that produced `token`; None is a no-op.

    `TaskHandle.__exit__` calls this from a `finally` whatever happened, and
    the token is None both when OTel is absent and when the handle held no
    span to attach — so the guard lives here rather than at the call site.
    """
    if otel_context is None or token is None:
        return
    otel_context.detach(token)  # type: ignore[arg-type]


def record_failure(span: Span, exc: BaseException) -> None:
    """Mark `span` failed.

    Choosing `start_span()` over `start_as_current_span()` — necessary,
    because the latter is a `@contextmanager` and cannot back a handle that
    may outlive one frame — gives up its `record_exception=True` and
    `set_status_on_exception=True` defaults, so this does that work by hand.
    """
    if otel_trace is None:
        return
    span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
    span.record_exception(exc)
