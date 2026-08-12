# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository (`lumberjack`, hosted as `mikewitt/paul-bunyan`) is pre-scaffolding: as of this writing it contains no source code, only this file. Everything below describes the intended design that future work should implement and conform to, not code that exists yet. Do not assume any module, file, or directory mentioned below exists until you've checked.

## Vision

`lumberjack` is a drop-in UX layer for Python's stdlib `logging`. The premise: developers scatter `logger.debug(...)` calls through code while building it, then delete or bury them once things work — but a log line that recurs inside a loop is progress signal, not noise. Instead of deleting them, `lumberjack` intercepts them: it captures every record into a queryable store (full fidelity, never lost), while rendering a live progress bar in place of a thousand scrolling lines. Concurrency (threads, processes, asyncio tasks) falls out of the same mechanism — each source's ticks are attributed at write time, so multiple workers become multiple progress bars for free.

The intended experience is a value ladder:
- **Drop it in** (`import lumberjack; lumberjack.init()`) — existing log lines become progress display, attributed per worker, no other code changes.
- **Instrument a little more** (`lumberjack.track()` / `lumberjack.task()`, or a hints config) — exact progress, named tasks, task hierarchy, where you bother to say so.
- Nothing in between is required. Uninstrumented code still works; instrumented code just looks better.

## Design principles

1. **Zero-config first run.** `init()` visibly improves output with no other changes.
2. **Store, then render.** Records are captured into a store first; every renderer reads from that store rather than transforming the log stream inline. This is what allows a live TTY view and a plain file consumer simultaneously without divergent logic.
3. **Concurrency-aware by design.** Source attribution (thread/process/asyncio task) is captured as structured metadata at write time — never inferred later from message text.
4. **`init()` is for applications, never libraries.** Standard Python convention: apps configure logging, libraries attach a `NullHandler` and stay quiet. `init()` takes exclusive ownership of output (replaces existing root logger handlers by default, opt-out to layer instead). Corollary: the tracking API (`track`/`task`) must work *without* `init()` having been called — a library can use it and, absent initialization, it degrades to ordinary `logging`/OTel emission. No import-time or call-time dependency on `init()`.
5. **Never assume a human is watching.** Live-redraw output (ANSI, cursor control, in-place bars) is harmful when piped to a file or another program. Detect the consumer (TTY vs pipe/file) and pick a mode accordingly, with an explicit override.
6. **Lossy display, lossless store — and never corrupt a traceback.** The store→display path is deliberately lossy (that's the product: verbose logging in, concise progress out). The buffer→store path must not drop records. Separately: a live TTY display must be torn down cleanly on exit *and* on unhandled exception, before Python's excepthook prints — otherwise the traceback gets mangled by cursor control or overwritten by a redraw. An `atexit` dump of the last N records is a cheap diagnostic on top of that.
7. **Standard-library-native.** Built on `logging.Handler`/`Filter`/`LogRecord`. Never require replacing `logging` calls.
8. **Two install shapes.** Bare `pip install lumberjack` pulls in nothing — default SQLite backend is stdlib, so a library can instrument with zero imposed dependencies. `pip install lumberjack[recommended]` adds `rich`, which **is** the interactive display layer (this is the documented application install). `duckdb` and `opentelemetry-*` are separate opt-in extras.
9. **Every optional dependency degrades, never errors.** No `rich` → plain renderer. No OTel → span/metric wrappers become no-ops. No backend package → a clear error naming the extra to install. Each such case gets a "degrades gracefully" test.

## Engineering principles

- **Concise, auditable code.** Small readable modules over clever abstraction.
- **Test-driven development.** Tests pin the API shape before/alongside implementation — the test suite *is* the spec. Expect early scaffolding passes to include tests that fail against stub implementations by design.
- **Coverage via fixtures, not speculative code.** Don't add defensive code paths nothing exercises; high coverage should fall out of writing a fixture per edge case, not be chased separately.
- **Edge cases become GitHub issues, semi-automated,** via a form/template, an agent-validated repro step, then a filed issue linked back via an in-code marker (`# lumberjack: see issue #NN`).

## Architecture

Data flows one direction: **capture → buffer → store → (analysis) → render.** `LumberjackHandler` (plus the optional `OTelBridge`) is the only writer. Analysis, hints, and rendering are all readers that never touch the backing store representation directly — they go through the `RecordStore` interface.

### Components

- `lumberjack.init()` — installs the handler, takes ownership of the root logger, starts the store and renderer(s).
- `Session` — the components one `init()` owns (handler, store, renderer, output mode, pump) plus what `shutdown()` must put back (the root logger's prior handlers and level). `init()` and `teardown` share one instance rather than each keeping their own copies; `_session is None` is the single "is lumberjack running" question.
- `LumberjackHandler` — `logging.Handler` subclass; writes to a bounded write buffer (`collections.deque`) with source attribution. The bound is real: overflow evicts unread records, so the handler counts them and teardown reports the total at exit.
- `RecordStore` — pluggable interface (SQLite default/stdlib, DuckDB optional) behind the shared write buffer. Roughly: `append(records)`, `recent(n | since)`, `count_by_template(window)`, `count_by_source(window)`, `evict(before | keep_last)`, `templates()`. One implementation, two SQL dialects, one parametrized test suite across installed backends. `recent(n)` returns the last n oldest-first, which is also what the exit dump reads — there is deliberately no separate `tail()`.
- Tracking API (`lumberjack.track`, `lumberjack.task`) — explicit progress reporting, writes into the same store, works with or without `init()` (see Design Principle 4). `task()` mirrors an OTel span; `track()` mirrors `tqdm`. Object model: `task(...)` returns a handle that is itself the context manager (`__enter__` returns `self`); `.subtask()` returns the same type for nesting.
- `RepetitionAnalyzer` — clusters records by inferred template (numbers/UUIDs/paths masked out), tracks counts/rates, infers progress. Two-tier update model: cheap per-record metric updates inline, expensive pattern discovery (template extraction/clustering) runs periodically, never per record.
- `HintsConfig` — declarative config where the user explains message semantics (progress step, task boundary, noise, severity override); sits above the analyzer, so declared meaning beats inference.
- `OutputModeDetector` — interactive TTY vs plain/structured (file or pipe), with explicit override.
- `Renderer` — abstract interface; `RichTerminalRenderer` (optional-dependency guarded, falls back to plain if `rich` is absent or not a TTY) and `PlainTextRenderer` (plain text and JSON-lines, write-through — not timer-gated). Live TTY redraw is periodic (~200ms), timer-driven, decoupled from log volume; non-TTY output is always write-through.
- `OTelBridge` — optional `SpanProcessor`/`MetricReader` feeding the same store (inbound direction; outbound is `task()` emitting spans directly).

### Record schema

Derived from `logging.LogRecord`'s standard attributes (`name`, `levelname`, `levelno`, `msg`, `args`, `pathname`, `lineno`, `funcName`, `created`, `thread`, `threadName`, `process`, `processName`, `exc_info`, ...) plus lumberjack's own columns: asyncio task name (where determinable), task id / parent task id (from the tracking API, for hierarchy), template id (assigned once repetition analysis identifies a recurring shape).

### Storage backends

| Backend | Dependency | Notes |
|---|---|---|
| `sqlite` (default) | stdlib | Zero required deps. Raw `sqlite3`, not an ORM. WAL mode may support multiple writer processes, which could remove most multiprocessing IPC work — needs prototyping before building a queue-based fallback. |
| `duckdb` | optional extra | Columnar/OLAP, much faster grouping/windowing at scale. Single-writer, so multiprocessing still needs the queue path when this backend is selected. |

Retention target: in-memory by default (`:memory:`), ~1M records working target. Eviction must maintain derived progress state incrementally, not by recomputing from the full window.

### Integration surfaces

`task`/`track` mirror existing concepts on purpose rather than inventing vocabulary: `task()` ↔ an OTel span, `track()` ↔ `tqdm`. Libraries calling these impose no dependency and no runtime cost on downstream users — what they become (a real progress bar, a real OTel span, or plain log lines) is decided entirely by what the *application* installs and whether it calls `init()`. Known incompatibility until interception lands (post-1.0): `tqdm` writes its own ANSI/cursor control and will fight a live lumberjack display if both run concurrently.

## Phased plan (high level)

Full detail lives in the project plan; phase order is deliberate (simplest-first, exact-before-inferred):

0. Foundations — repo scaffolding, packaging, CI, pre-commit, test harness before implementation.
1. MVP — capture/store/render skeleton, including a throwaway crude end-to-end proof (one hardcoded/naively-detected repeating log shape rendered as a live bar) to validate the core premise early.
2. Tracking API (`track`/`task`), outbound OTel only — establishes the progress/task model before inference is built on top of it.
3. Multiprocessing-aware capture — prototype SQLite WAL multi-writer before building an IPC/queue fallback.
4. Repetition analysis & inferred progress — the phase that delivers the core premise (recurring log line → progress tick) on top of the Phase 1-3 substrate.
5. Hints config.
6. OpenTelemetry integration, inbound bridge (spans/metrics from other instrumented libraries).
7. Polish & extensibility (renderer interface finalized, theming, performance pass, docs).
8. (Post-1.0) Progress provider interception — monkeypatch `tqdm.tqdm` at `init()`.
9. (Stretch) Web renderer & disk-backed persistence.
10. (Stretch) Profiling & contingent Rust migration for a specific bottleneck, gated on profiling data — not scheduled work.

## Toolchain (intended, per decisions log)

- **Environment/deps:** `uv`
- **Lint/format:** `ruff` + `black`
- **Tests:** `pytest` + coverage; a single parametrized suite exercises every installed `RecordStore` backend
- **License:** Apache 2.0
- **Python floor:** 3.12+

Once scaffolding lands, expect `uv sync`, `uv run pytest`, `uv run ruff check .`, and `uv run black --check .` to be the standard local commands — verify against the actual `pyproject.toml` rather than assuming these exact invocations, since extras/scripts may refine them.
