# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository (`lumberjack`, hosted as `mikewitt/paul-bunyan`) has **Phases 0, 1 and 2 complete, and Phase 4a done**. Trunk is `daddy`, not `main`.

What exists and works: capture (`LumberjackHandler`), storage (`SQLiteRecordStore`), output-mode detection, plain/JSON/rich rendering, the Phase 1 live progress bar, the flush pump, `atexit`/excepthook teardown, and the Phase 2 tracking API (`task()`, `track()`, `TaskHandle.subtask()`) with outbound OTel spans. Grouping for the bar is by *source location*, which Phase 4 keeps as the identity axis rather than replacing — what it adds is containment analysis on top (see `RepetitionAnalyzer` below).

Phase 4a is done: `task()` and `track()` now draw named bars — determinate when a total was given, pulsing when not, indented by task depth, finishing on the `end` row. They sit above the source-location bars in one shared `Live`. No inference is involved; every number came from an instrumented call that stated it.

What does not exist yet, and must not be described as though it does: `RepetitionAnalyzer`, `HintsConfig`, the inbound `OTelBridge`, OTel metrics, the DuckDB backend, and multiprocessing-aware capture. Sections below describe the intended design for those. Check before assuming any module named here is on disk.

Everything is pre-1.0 with no released version and no back-compat obligation, so a wrong API shape should be fixed rather than deprecated.

## Vision

`lumberjack` is a drop-in UX layer for Python's stdlib `logging`. The premise: developers scatter `logger.debug(...)` calls through code while building it, then delete or bury them once things work — but a log line that recurs inside a loop is progress signal, not noise. Instead of deleting them, `lumberjack` intercepts them: it captures every record into a queryable store (full fidelity, never lost), while rendering a live progress bar in place of a thousand scrolling lines. Concurrency (threads, processes, asyncio tasks) falls out of the same mechanism — each source's ticks are attributed at write time, so multiple workers become multiple progress bars for free.

**Log density is input quality, and that inverts the usual advice.** The signal lumberjack reads is the *shape of the event stream* — which source locations fire, in what order, how often — so more diagnostic ticks mean better cycle resolution, not more noise. The instinct to delete debug lines once the code works is removing exactly what the display is built from. The pitch is not "we tolerate your log spam"; it is "keep it, and add more."

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
10. **An 80% solution, deliberately.** Inference will be wrong in plenty of situations, and that is acceptable: the goal is a usable-if-imperfect UX you can drop on top of code you did not write. Tools and documentation take a given codebase from 80% to 95%; nothing takes it to 100%, and chasing that is how this never ships. This is Principle 6's line extended to inference — **the display may be wrong; the record never is.** A bar that guesses a total and blows past it is a cosmetic miss. A record that never reached the store is a bug. Anything that would sacrifice the second to improve the first is refused; anything that improves the first and is merely approximate is fine. In practice this licenses: shipping cycle detection that misfires on high-variance loops, preferring a cheap estimate over a correct one, and closing whole classes of hostile input (see issue #37) with a documented limitation plus a tool rather than a fix.

## Engineering principles

- **Concise, auditable code.** Small readable modules over clever abstraction.
- **Test-driven development.** Tests pin the API shape before/alongside implementation — the test suite *is* the spec. Expect early scaffolding passes to include tests that fail against stub implementations by design.
- **Coverage via fixtures, not speculative code.** Don't add defensive code paths nothing exercises; high coverage should fall out of writing a fixture per edge case, not be chased separately.
- **An idea that surfaces mid-implementation becomes an issue, not a detour.** File it in the same commit that provoked it, with the milestone its scope boundary above dictates, and carry on with what you were doing. This is already how #31–#35, #37, #38 and #40 came to exist; writing it down makes it something a review can enforce. The exception is narrow and must be argued explicitly in the commit message: a fix belongs in the current change only when it is in lines that change anyway *and* the current work makes the defect worse.
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
- Tracking API (`lumberjack.track`, `lumberjack.task`) — explicit progress reporting. When a session exists its events become `logging` records that travel the normal handler→buffer→store path, so the handler stays the only writer; with no session it emits nothing (see Design Principle 4). `task()` mirrors an OTel span; `track()` mirrors `tqdm`. Object model: `task(...)` returns a handle that is itself the context manager (`__enter__` returns `self`); `.subtask()` returns the same type for nesting. `otel.py` is the only module importing `opentelemetry`, guarded, and exposes `tracer()` as a *function* so one monkeypatched seam forces the absent path on every CI job.
- `RepetitionAnalyzer` — infers loop structure from the event stream. **Identity is source location, not message text**: a call at `foo.py:6` is the same call on every iteration, exactly, with no inference and no masking. Message content is enrichment; the primary signal is temporal and structural. Two-tier update model: cheap per-record metric updates inline, expensive structure discovery periodically, never per record. It reads *ordinary* log records — `task_event` rows are ground truth to be trusted directly, never evidence to infer from, because Phase 2 samples its ticks and inferring a period from a sampled stream produces a fictional one.

  Three measurements, in increasing difficulty, and each useful before the next exists:

  | Question | From | Cost |
  |---|---|---|
  | How fast is this loop iterating? | inter-arrival times for one source | nearly free today |
  | How many iterations so far? | count for one source | free today |
  | Are two sources the same loop, or nested? | rate ratio + interleaving | the real work |
  | How long until done? | a total, which rate alone never gives | see below |

  A source's own recurrence interval *is* its loop's period — no clustering needed to time a loop, only to decide how many bars to draw. That split means rate-and-count can ship before containment analysis without rework.

- **Totals, and the ratio that yields them.** Rate says how fast, never how far. The ratio between two sources' rates classifies their relationship: ~1:1 with a stable phase offset means one loop body (merge into one bar); ~N:1 with B always falling between two A's means B is nested inside A; no stable ratio means unrelated. The N in an N:1 ratio **is the inner loop's total** — containment and the total come from the same measurement. That also gives the promotion rule: a total cannot be known until one cycle completes, and once it does it is known, so a loop is a pulsing spinner through cycle 1 and a determinate bar from cycle 2 onward.
- `HintsConfig` — declarative config where the user explains message semantics (progress step, task boundary, noise, severity override); sits above the analyzer, so declared meaning beats inference.
- `OutputModeDetector` — interactive TTY vs plain/structured (file or pipe), with explicit override.
- `Renderer` — abstract interface; `RichTerminalRenderer` (optional-dependency guarded, falls back to plain if `rich` is absent or not a TTY) and `PlainTextRenderer` (plain text and JSON-lines, write-through — not timer-gated). Live TTY redraw is periodic (~200ms), timer-driven, decoupled from log volume; non-TTY output is always write-through.
- `RepeatingSourceModel` — accumulates `count_by_source_since()` forward from a watermark. Totals are therefore **monotonic by construction**: `evict()` can drop the rows a bar counted without the bar counting backwards, which is what a progress bar has to mean. Bar count is unbounded on purpose — see below.
- `OTelBridge` — optional `SpanProcessor`/`MetricReader` feeding the same store (inbound direction; outbound is `task()` emitting spans directly).

### Decisions worth not relitigating

- **A high bar count is a symptom, not a display bug.** It means grouping is too granular or the code logs ungroupably. Capping by default, or collapsing the excess into a neutral "… 298 more" row, destroys that signal. The diagnosis is phase-dependent: under today's source-location grouping, 800 bars is an honest report of 800 busy call sites; under Phase 4 it would mean containment analysis has not merged sibling call sites into shared loops, or that finished bars are not retiring — so the count is a measure of how much structure has *not* been inferred yet, which is precisely the signal a cap would hide. `LUMBERJACK_MAX_BARS` exists only as a debug/terminal-compat escape hatch — opt-in, absent from the README, reported once at exit. Issue #8 is the home for the real fix.
- **Two bar kinds share one `Live`; they are never two started `Progress` objects.** `Progress.start()` starts a `Live` of its own, and rich allows only one live per console: the second becomes `_nested`, at which point its `refresh()` re-renders *the root's* renderable and returns, so its bars never draw (verified against rich 15.0.0, `Live.refresh`). Giving each its own `Console` is worse — two Lives writing cursor control to one stderr corrupt the frame. The working shape is rich's documented one: the renderer owns a single `Live(Group(task_progress, source_progress))` and **neither `Progress` is started**. Exact bars go in the group first, because the ellipsis crops bottom-up and instrumented bars must not lose their slots to inferred ones. Owning the `Live` is also where issue #28's bounded final frame belongs.
- **Rich crops rather than corrupts.** `Progress` runs `Live` with `vertical_overflow="ellipsis"`, so an over-tall live frame shows the first N bars plus an ellipsis with correct cursor arithmetic. Do not justify display work by claiming otherwise. `Live.stop()` does *not* crop, though — see issue #28.
- **At exit, drain before closing the display.** A live bar draws its closing frame from the store, so `teardown.run()` flushes the buffer first; closing first leaves the final count short, or with the pump disabled draws no bar at all. The excepthook path is the opposite by design — there a traceback is imminent, so the display comes down first.
- **Pulse means "no claim", and confidence only ever increases.** `add_task(total=None)` gives rich an indeterminate, pulsing bar, and that one mechanism carries all the uncertainty the display needs: pulsing before a cycle is detected (work is happening, nothing more is claimed), determinate once a total is known, and **pulsing again if the count exceeds the estimate** — degrading to honesty rather than showing 127% or freezing at 100%. Promotion is one-way, for the same reason bars never move: a display that oscillates between spinner and bar as confidence wobbles is worse than one that stays a spinner.
- **A bar retires on idle relative to its own period,** not on any completion signal — there isn't one. Absence of events is ambiguous (a slow iteration and a finished loop look identical), because nothing raises `StopIteration` at a log line. Roughly 10× the measured interval with no event is the 80% answer: wrong for a loop with wildly varying iterations, right nearly always, and cheap. Do not hold out for a correct answer here; there is no signal that would provide one.
- **Nesting depth is derived, never declared.** `tqdm` makes the author pass `position=` and manage `leave=` by hand, so depth is static and known at authoring time — which breaks on recursion, on a function that is sometimes top-level and sometimes inside a loop, and with threads. Deriving depth from observed containment removes the bookkeeping *and* covers the cases tqdm structurally cannot, because depth becomes a property of what happened rather than something declared in advance. This is the strongest form of the pitch and should not be traded away for implementation convenience.
- **Bar display order is append-only, first-qualified.** Bars never move once placed, because a bar that jumps around as counts overtake each other is unreadable. The known cost is that with cropping the visible window is permanently the earliest qualifiers, not the busiest — an open question under issue #8.
- **A `TaskHandle` parents only inside a `with`,** and out-of-order resets are handled rather than prevented. Only `__enter__` sets the ambient contextvar and only `__exit__` clears it, so a handle used bare holds no token and cannot be the one whose reset misfires. That is *not* enough to make out-of-order resets impossible: `with` is LIFO only within one frame, two suspended generators each holding one interleave freely, and `ContextVar.reset()` does **not** raise for an out-of-order token from the same Context — it silently writes the old value back. So the ambient slot is never trusted directly. `_unbind()` resets only while still the current binding, and `_ambient_parent()` walks past any handle that has already ended, following where each was *entered* rather than where it was created. Both guards are load-bearing and separately mutation-tested; a bug was found here twice.
- **`.subtask()` takes its parent as `self`,** never from ambient context, because contextvars propagate into asyncio tasks but **not** into a bare `threading.Thread`. It must also build the child's OTel context from `self._span`, or the log and span hierarchies disagree in exactly that threaded case.
- **`GeneratorExit` is not a task failure.** It is what a user's generator receives when the consumer stops early, so a plain `break` must not put a traceback on the ERROR channel — the one line a user is guaranteed to see — or mark the span failed. `track()`'s own early `break` already records a clean end; a bare `with` inside a generator has to agree.
- **Progress ticks are sampled; the `end` row is not.** `advance()` emits at most one record per 50ms. The unconditional reason is that non-TTY output is write-through, one line per record, so per-item ticks print a million lines for a million-item loop — the disease the package exists to cure, caused by the cure. A second reason is real but machine-dependent and should not be quoted as a law: an unsampled loop measures ~59,000 records/s through the handler alone and ~24,000 with the JSON-lines renderer, against a 50,000/s drain ceiling, so fast hardware overflows lumberjack's own buffer and trips its own dropped-records warning while slower hardware does not. (An earlier note here claimed 85,000/s unconditionally; it did not reproduce — measure before quoting.) Nothing is lost either way, because `progress_current` is **absolute**, not a delta, and `end()` writes the final count unsampled. Do not "fix" a bar that looks coarse by lowering the interval.
- **Spans are attached unconditionally, never gated on `is_recording()`.** That is also False for a span the provider sampled out, so gating on it breaks parent/child propagation under head sampling — silently, and only in production.

### Record schema

Derived from `logging.LogRecord`'s standard attributes (`name`, `levelname`, `levelno`, `msg`, `args`, `pathname`, `lineno`, `funcName`, `created`, `thread`, `threadName`, `process`, `processName`, `exc_info`, ...) plus lumberjack's own columns: asyncio task name and asyncio task id (where determinable), task id / parent task id (from the tracking API, for hierarchy), the four progress columns the tracking API writes (`task_label`, `task_event` — `start`/`update`/`end` — `progress_current`, `progress_total`), template id (reserved, always NULL today; message-shape analysis is a Phase 4 refinement and a wrapper diagnostic, not the grouping key).

The tracking API reaches those columns through **one** `extra=` key: `extra={"lumberjack": TaskEvent(...)}`, a frozen dataclass read back under an `isinstance` guard, so a foreign `record.lumberjack` from another library degrades to "no task data" rather than crashing `emit()` for every record in the process. One collision surface instead of four — and `taskName` already collided once, which is why asyncio attribution is `asyncio_*`.

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
2. **Done.** Tracking API (`track`/`task`), outbound OTel only — establishes the progress/task model before inference is built on top of it.
3. Multiprocessing-aware capture — prototype SQLite WAL multi-writer before building an IPC/queue fallback. **Deferred behind Phase 4 — see execution order below.**
4. **4a done, 4b next.** Repetition analysis & inferred progress — the phase that delivers the core premise (recurring log line → progress tick). Structure first (rate, count, containment, nested bars); message-text analysis is a later refinement inside the phase, not its basis. Runs in two halves, exact before inferred:
   - **4a — Done.** Named determinate bars from stored tracking data, no inference at all. `TaskProgressModel` folds `store.task_events_since()` into one bar per task; the renderer draws them above the source bars in one shared `Live`.
   - **4b — the inference proper** (issue #38): per-source rate and count, containment from rate ratios, pulse→promote, idle retirement. Reuses 4a's display model rather than inventing one.
5. Hints config.
6. OpenTelemetry integration, inbound bridge (spans/metrics from other instrumented libraries).
7. Polish & extensibility (renderer interface finalized, theming, performance pass, docs).
8. (Post-1.0) Progress provider interception — monkeypatch `tqdm.tqdm` at `init()`.
9. (Stretch) Web renderer & disk-backed persistence.
10. (Stretch) Profiling & contingent Rust migration for a specific bottleneck, gated on profiling data — not scheduled work.

### Phases are GitHub milestones

Phases 0–7 exist as milestones. **The milestone number is the phase number plus one** (Phase 0 is milestone 1, Phase 7 "Polish & extensibility" is milestone 8) — the API takes the number, not the title, so assign one issue and read it back before batching. Phases 8–10 have no milestone yet; nothing maps to them.

File new work against its phase. Defects in already-shipped code and anything genuinely ambiguous go to **Polish & extensibility** — correcting minor defects is what polish means — rather than being left unassigned or forced into a phase they do not belong to.

**Execution order is not phase order.** Phase 4 is being built before Phase 3. Nothing in Phase 4 touches the write path — it adds *readers* — while Phase 3 changes how records get into the store, so there is no collision and no rework. Multiprocessing capture is also infrastructure whose payoff is *more bars*, which is worth little until the bars themselves are worth multiplying. The numbers and milestones are deliberately left as they are: renumbering would invalidate "Phase 4" in every issue body that already cites it, for no gain. **The SQLite WAL multi-writer question is a one-day spike, not a phase** — if WAL genuinely supports multiple writer processes, most of Phase 3 evaporates, so run the spike opportunistically and record the answer as a milestone comment.

### What each milestone is, and is not

Written down so "does this belong here?" stops being a judgement call.

| Phase | IN | OUT |
|---|---|---|
| **3 — Multiprocessing capture** | Records from child processes reaching the parent's store (WAL multi-writer if the spike says yes, otherwise a queue path). The attribution columns already exist. | Any display change. DuckDB multi-process — the queue path is its answer by design. Anything networked. |
| **4 — Repetition analysis** | 4a's named determinate bars from stored tracking data; then rate/count, containment ratios, pulse→promote, idle retirement (#38). Also #26, #32, #35, and #8's real fix. #37 as detection plus diagnostic only. | Message-template masking beyond the wrapper diagnostic. Hints. Caller fingerprinting (#37's later options). Multiprocessing. |
| **5 — Hints config** | Declarative config that overrides inference: progress step, task boundary, noise, severity. | New inference of any kind. Runtime API surface beyond reading the config. |
| **6 — OpenTelemetry** | The inbound `OTelBridge` (SpanProcessor/MetricReader → store) and the `trace_id`/`span_id` columns (#34). | Outbound spans — Phase 2 shipped them. Exporter or sampling configuration. Rendering metrics beyond the existing bars. |
| **7 — Polish & extensibility** | Defects in already-shipped code (#7, #11, #14, #15, #16, #18, #31, #33), renderer interface freeze, theming, performance pass, docs. | New inference or capability work. Anything owned by an earlier phase. |

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
| `coverage-badge` | One suite run with `--cov-report=xml`, feeding two consumers: the Codacy coverage upload on every event, and the committed badge on trunk pushes only. Name kept despite doing both — see below |

CodeQL and Codacy also run, both configured outside this workflow.

Coverage is uploaded to Codacy from the `coverage-badge` job. It comes from that one ubuntu/3.12 run rather than all six `test` legs: the union across legs would be marginally higher — the bare-install `skipif`, Windows path branches — but collecting it means six `--partial` uploads plus a `final` call, which is a lot of workflow for a fraction of a percent. The upload step is skipped, not failed, when `CODACY_PROJECT_TOKEN` is absent, which is the case for fork pull requests and for anyone who cloned this without a Codacy project.

Codacy's bandit engine skips `tests/` — see `.codacy.yaml`, which records why per finding. The short version: 437 of its 447 findings were `assert` used in a pytest suite, where the assert *is* the test, and the rest of the test-only findings were subprocess launches and fake `/tmp` pathnames in row fixtures. `src/` and `examples/` stay in scope, so the ten remaining findings are ones somebody has read and kept. Individual patterns can only be turned off in Codacy's web UI, so path scoping is all the file can do.

**Every action is pinned to a commit SHA, with its version in a trailing comment.** A git tag is moveable: whoever owns the action can repoint `v4` at different code and every run picks it up with no diff and no review. That matters here because CI holds `contents: write` for the badge and a Codacy token. Bump with `git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<tag>^{}'` — the `^{}` dereferences an annotated tag to the commit. The pins are currently unmanaged, so they will go stale; issue #42 holds the Renovate-or-Dependabot decision that would fix that.

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
