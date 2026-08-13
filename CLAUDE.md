# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository (`lumberjack`, hosted as `mikewitt/paul-bunyan`) has **Phases 0 and 1 complete**. Trunk is `daddy`, not `main`.

What exists and works: capture (`LumberjackHandler`), storage (`SQLiteRecordStore`), output-mode detection, plain/JSON/rich rendering, the Phase 1 live progress bar, the flush pump, and `atexit`/excepthook teardown. Grouping for the bar is by *source location* — a deliberate placeholder for Phase 4's template clustering.

What does not exist yet, and must not be described as though it does: `track()` / `task()`, `RepetitionAnalyzer`, `HintsConfig`, `OTelBridge`, the DuckDB backend, and multiprocessing-aware capture. Sections below describe the intended design for those. Check before assuming any module named here is on disk.

Everything is pre-1.0 with no released version and no back-compat obligation, so a wrong API shape should be fixed rather than deprecated.

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
4. **`init()` is for applications, never libraries.** Standard Python convention: apps configure logging, libraries attach a `NullHandler` and stay quiet. `init()` takes exclusive ownership of output (replaces existing root logger handlers by default, opt-out to layer instead). Corollary: the tracking API (`track`/`task`) must work *without* `init()` having been called — a library can use it and, absent initialization, it is **inert**: no store to write to, so nothing is written, and no log line either. What it produces is decided by two independent switches the *application* owns, never the library:

   | OTel configured? | `init()` called? | `task()` produces |
   |---|---|---|
   | no | no | nothing |
   | yes | no | OTel spans |
   | no | yes | records in the store, and thence the display |
   | yes | yes | both |

   OTel spans depend on **OTel's** configuration, never on `init()` — an unconfigured OTel no-ops through its own `NoOpTracer`, so lumberjack adds no gating of its own. Calling the tracking API before `init()` is legal, cheap, and never raises; there is no import-time or call-time *requirement* that `init()` has run. (Emitting task events as ordinary log lines when there is no session is deliberately *not* the default — it would print a library's instrumentation into any host app that configured logging. See issue #31.)
5. **Never assume a human is watching.** Live-redraw output (ANSI, cursor control, in-place bars) is harmful when piped to a file or another program. Detect the consumer (TTY vs pipe/file) and pick a mode accordingly, with an explicit override.
6. **Lossy display, lossless store — and never corrupt a traceback.** The store→display path is deliberately lossy (that's the product: verbose logging in, concise progress out). The buffer→store path must not drop records. Separately: a live TTY display must be torn down cleanly on exit *and* on unhandled exception, before Python's excepthook prints — otherwise the traceback gets mangled by cursor control or overwritten by a redraw. An `atexit` dump of the last N records is a cheap diagnostic on top of that.
7. **Standard-library-native.** Built on `logging.Handler`/`Filter`/`LogRecord`. Never require replacing `logging` calls.
8. **Two install shapes.** Bare `pip install lumberjack` pulls in nothing — default SQLite backend is stdlib, so a library can instrument with zero imposed dependencies. `pip install lumberjack[recommended]` adds `rich`, which **is** the interactive display layer (this is the documented application install). `duckdb` and `opentelemetry-*` are separate opt-in extras.
9. **Every optional dependency degrades, never errors.** No `rich` → plain renderer. No OTel → span/metric wrappers become no-ops. No backend package → a clear error naming the extra to install. Each such case gets a "degrades gracefully" test.

## Engineering principles

- **Concise, auditable code.** Small readable modules over clever abstraction.
- **Test-driven development.** Tests pin the API shape before/alongside implementation — the test suite *is* the spec. Expect early scaffolding passes to include tests that fail against stub implementations by design.
- **Coverage via fixtures, not speculative code.** Don't add defensive code paths nothing exercises; high coverage should fall out of writing a fixture per edge case, not be chased separately.
- **Edge cases become GitHub issues, semi-automated,** via a form/template, an agent-validated repro step, then a filed issue linked back via an in-code marker (`# lumberjack: see issue #NN`). This is in force — grep the marker to find known-incomplete code. Remove the marker in the same change that closes the issue.
- **Measure before claiming a speedup, and measure again after.** Two changes in this repo were only ~3x until the query plan was checked; both are commented with the number and the reason. `EXPLAIN QUERY PLAN` is cheap and has already twice contradicted a design that looked obviously correct.
- **Assert the observable contract, not internal state.** Where a test could read a state accessor or check the behaviour that accessor exists to describe, prefer the behaviour — several accessors were deleted precisely because tests were their only caller.

## Architecture

Data flows one direction: **capture → buffer → store → (analysis) → render.** `LumberjackHandler` (plus the optional `OTelBridge`) is the only writer. Analysis, hints, and rendering are all readers that never touch the backing store representation directly — they go through the `RecordStore` interface.

### Components

- `lumberjack.init()` — installs the handler, takes ownership of the root logger, starts the store and renderer(s).
- `Session` — the components one `init()` owns (handler, store, renderer, output mode, pump) plus what `shutdown()` must put back (the root logger's prior handlers and level). `init()` and `teardown` share one instance rather than each keeping their own copies of its parts. The instance lives in `session.py`, and `current_session() is None` is the single "is lumberjack running" question. `teardown` deliberately keeps its *own* reference to it, as an install token: it is installed and uninstalled on its own lifecycle, and its whole test suite drives it with fakes and no `init()` at all.
- `LumberjackHandler` — `logging.Handler` subclass; writes to a bounded write buffer (`collections.deque`) with source attribution. The bound is real: overflow evicts unread records, so the handler counts them and teardown reports the total at exit.
- `RecordStore` — pluggable interface (SQLite default/stdlib, DuckDB optional) behind the shared write buffer. Roughly: `append(records)`, `recent(n | since)`, `count_by_template(window)`, `count_by_source(window)`, `count_by_source_since(after_id)`, `evict(before | keep_last)`, `templates()`. One implementation, two SQL dialects, one parametrized test suite across installed backends. `recent(n)` returns the last n oldest-first, which is also what the exit dump reads — there is deliberately no separate `tail()`. `count_by_source_since()` is the one anything on a timer should call: it groups only rows past a watermark, so a redraw costs what arrived rather than what the store holds.
- Tracking API (`lumberjack.track`, `lumberjack.task`) — explicit progress reporting. When a session exists its events become `logging` records that travel the normal handler→buffer→store path, so the handler stays the only writer; with no session it emits nothing (see Design Principle 4). `task()` mirrors an OTel span; `track()` mirrors `tqdm`. Object model: `task(...)` returns a handle that is itself the context manager (`__enter__` returns `self`); `.subtask()` returns the same type for nesting.
- `RepetitionAnalyzer` — clusters records by inferred template (numbers/UUIDs/paths masked out), tracks counts/rates, infers progress. Two-tier update model: cheap per-record metric updates inline, expensive pattern discovery (template extraction/clustering) runs periodically, never per record.
- `HintsConfig` — declarative config where the user explains message semantics (progress step, task boundary, noise, severity override); sits above the analyzer, so declared meaning beats inference.
- `OutputModeDetector` — interactive TTY vs plain/structured (file or pipe), with explicit override.
- `Renderer` — abstract interface; `RichTerminalRenderer` (optional-dependency guarded, falls back to plain if `rich` is absent or not a TTY) and `PlainTextRenderer` (plain text and JSON-lines, write-through — not timer-gated). Live TTY redraw is periodic (~200ms), timer-driven, decoupled from log volume; non-TTY output is always write-through.
- `RepeatingSourceModel` — accumulates `count_by_source_since()` forward from a watermark. Totals are therefore **monotonic by construction**: `evict()` can drop the rows a bar counted without the bar counting backwards, which is what a progress bar has to mean. Bar count is unbounded on purpose — see below.
- `OTelBridge` — optional `SpanProcessor`/`MetricReader` feeding the same store (inbound direction; outbound is `task()` emitting spans directly).

### Decisions worth not relitigating

- **A high bar count is a symptom, not a display bug.** It means grouping is too granular or the code logs ungroupably. Capping by default, or collapsing the excess into a neutral "… 298 more" row, destroys that signal. The diagnosis is phase-dependent: under today's source-location grouping, 800 bars is an honest report of 800 busy call sites; under Phase 4's template grouping it would mean 800 distinct message *shapes*, which points at masking having failed. `LUMBERJACK_MAX_BARS` exists only as a debug/terminal-compat escape hatch — opt-in, absent from the README, reported once at exit. Issue #8 is the home for the real fix.
- **Rich crops rather than corrupts.** `Progress` runs `Live` with `vertical_overflow="ellipsis"`, so an over-tall live frame shows the first N bars plus an ellipsis with correct cursor arithmetic. Do not justify display work by claiming otherwise. `Live.stop()` does *not* crop, though — see issue #28.
- **At exit, drain before closing the display.** A live bar draws its closing frame from the store, so `teardown.run()` flushes the buffer first; closing first leaves the final count short, or with the pump disabled draws no bar at all. The excepthook path is the opposite by design — there a traceback is imminent, so the display comes down first.
- **Bar display order is append-only, first-qualified.** Bars never move once placed, because a bar that jumps around as counts overtake each other is unreadable. The known cost is that with cropping the visible window is permanently the earliest qualifiers, not the busiest — an open question under issue #8.

### Record schema

Derived from `logging.LogRecord`'s standard attributes (`name`, `levelname`, `levelno`, `msg`, `args`, `pathname`, `lineno`, `funcName`, `created`, `thread`, `threadName`, `process`, `processName`, `exc_info`, ...) plus lumberjack's own columns: asyncio task name and asyncio task id (where determinable), task id / parent task id (from the tracking API, for hierarchy), template id (assigned once repetition analysis identifies a recurring shape).

### Storage backends

| Backend | Dependency | Notes |
|---|---|---|
| `sqlite` (default) | stdlib | Zero required deps. Raw `sqlite3`, not an ORM. WAL mode may support multiple writer processes, which could remove most multiprocessing IPC work — needs prototyping before building a queue-based fallback. |
| `duckdb` | optional extra | Columnar/OLAP, much faster grouping/windowing at scale. Single-writer, so multiprocessing still needs the queue path when this backend is selected. |

Retention target: in-memory by default (`:memory:`), ~1M records working target. Eviction must maintain derived progress state incrementally, not by recomputing from the full window — satisfied for the bar model by `count_by_source_since()`, and any future reader on a timer owes the same.

Two SQLite specifics that measurement forced, both commented at their call sites. `count_by_source_since()` uses `NOT INDEXED`: left alone SQLite serves the `GROUP BY` from `idx_records_source` as a covering index, scanning every row to answer a delta query, and ruling the index out turns it into a rowid range seek (32ms → 0.5ms at 1M rows). `evict(keep_last)` resolves a cutoff id and deletes by range rather than `id NOT IN (SELECT …)`. A second backend will need its own answers to both — these are not portable.

### Integration surfaces

`task`/`track` mirror existing concepts on purpose rather than inventing vocabulary: `task()` ↔ an OTel span, `track()` ↔ `tqdm`. Libraries calling these impose no dependency and no runtime cost on downstream users — what they become (a real progress bar, a real OTel span, both, or nothing at all) is decided entirely by what the *application* installs and whether it calls `init()`. Known incompatibility until interception lands (post-1.0): `tqdm` writes its own ANSI/cursor control and will fight a live lumberjack display if both run concurrently.

## Phased plan (high level)

Full detail lives in the project plan; phase order is deliberate (simplest-first, exact-before-inferred):

0. **Done.** Foundations — repo scaffolding, packaging, CI, pre-commit, test harness before implementation.
1. **Done.** MVP — capture/store/render skeleton, including a throwaway crude end-to-end proof (one hardcoded/naively-detected repeating log shape rendered as a live bar) to validate the core premise early.
2. Tracking API (`track`/`task`), outbound OTel only — establishes the progress/task model before inference is built on top of it.
3. Multiprocessing-aware capture — prototype SQLite WAL multi-writer before building an IPC/queue fallback.
4. Repetition analysis & inferred progress — the phase that delivers the core premise (recurring log line → progress tick) on top of the Phase 1-3 substrate.
5. Hints config.
6. OpenTelemetry integration, inbound bridge (spans/metrics from other instrumented libraries).
7. Polish & extensibility (renderer interface finalized, theming, performance pass, docs).
8. (Post-1.0) Progress provider interception — monkeypatch `tqdm.tqdm` at `init()`.
9. (Stretch) Web renderer & disk-backed persistence.
10. (Stretch) Profiling & contingent Rust migration for a specific bottleneck, gated on profiling data — not scheduled work.

### Phases are GitHub milestones

Phases 0–7 exist as milestones. **The milestone number is the phase number plus one** (Phase 0 is milestone 1, Phase 7 "Polish & extensibility" is milestone 8) — the API takes the number, not the title, so assign one issue and read it back before batching. Phases 8–10 have no milestone yet; nothing maps to them.

File new work against its phase. Defects in already-shipped code and anything genuinely ambiguous go to **Polish & extensibility** — correcting minor defects is what polish means — rather than being left unassigned or forced into a phase they do not belong to.

## Toolchain

- **Environment/deps:** `uv`
- **Lint/format:** `ruff` lints, `black` formats — one tool per job. `ruff format` is deliberately not run; it would be a second opinion on the question `black` already answers.
- **Types:** `mypy`, strict over `src` and ordinary over `tests`/`examples`. Strict where it matters because the package ships `py.typed`, so a wrong annotation is our bug in someone else's build.
- **Tests:** `pytest` + coverage; a single parametrized suite exercises every installed `RecordStore` backend. Warnings are errors.
- **License:** Apache 2.0
- **Python floor:** 3.12+

The full local gate, which is what CI runs:

```bash
uv sync --all-extras
uv run ruff check . && uv run black --check .
uv run mypy --strict src && uv run mypy tests examples
uv run pytest
```

### CI

Jobs are independent — knowing *which* is broken beats making one wait on another, the same reasoning behind `fail-fast: false` on the matrix.

| Job | Guards |
|---|---|
| `lint` | Once, not six times — style is a property of the source, not the interpreter |
| `typecheck` | `mypy`, strict on the shipped package |
| `test` | 6 legs: {ubuntu, windows} × {3.12, 3.13, 3.14} |
| `bare install (no rich)` | Principle 8 — the zero-dependency install. Asserts `rich` is genuinely absent so it cannot rot into a duplicate of `test` |
| `package` | `uv lock --check`, builds the wheel, installs it into a clean venv, asserts `py.typed` ships |
| `coverage-badge` | Trunk pushes only; commits the badge with `[skip ci]` |

CodeQL also runs, configured outside this workflow.

Every job pins its interpreter with `actions/setup-python` *before* `setup-uv`. Without it `uv sync` resolves whatever satisfies `requires-python` and the matrix silently stops testing six versions — verified from run logs that the legs really do run distinct interpreters.

`.pre-commit-config.yaml` pins `ruff`/`black` by git rev while `uv` resolves them from `pyproject.toml`; nothing links the two, so `tests/test_toolchain.py` fails when they drift. Otherwise a commit passes locally and fails `lint` over a rule one version has and the other does not.

### Environment variables

| Variable | Effect |
|---|---|
| `LUMBERJACK_OUTPUT_MODE` | `rich` / `plain` / `json`, overriding TTY detection |
| `LUMBERJACK_MAX_BARS` | Opt-in ceiling on drawn bars. Debug/compat aid, deliberately undocumented in the README — see the decisions above |

Both follow the same rule: a bad **argument** is a caller's bug and raises; a bad **environment variable** is an operator typo, so it warns and degrades.

### Verifying display behaviour

Bars only render on a TTY, so piping the demo shows the plain renderer instead. To see the real thing, run it under a pty and strip ANSI. Note `examples/demo.py` calls `shutdown()`, which unregisters the `atexit` hook — a script that exits naturally is needed to observe exit-time diagnostics.
