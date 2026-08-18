# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository (`lumberjack`, hosted as `mikewitt/paul-bunyan`) has **Phases 0, 1, 2 and 4 complete**. Trunk is `daddy`, not `main`.

What exists and works: capture (`LumberjackHandler`), storage (`SQLiteRecordStore`), output-mode detection, plain/JSON/rich rendering, the flush pump, `atexit`/excepthook teardown, the Phase 2 tracking API (`task()`, `track()`, `TaskHandle.subtask()`) with outbound OTel spans, Phase 4's inference, and the row model on top of it. **Identity** is by *source location* and always has been; what changed is that the display no longer takes that as its unit too.

Phase 4a: `task()` and `track()` draw named bars — determinate when a total was given, pulsing when not, indented by task depth, finishing on the `end` row. They sit above the source-location bars in one shared `Live`. No inference is involved; every number came from an instrumented call that stated it.

Phase 4b: uninstrumented source bars are timed, structured and retired. `RepeatingSourceModel` measures each source's period, sorts sources by it to recover loop levels, and takes the ratio between an enclosing level and an enclosed one as the inner loop's total — worker-scoped, frozen once confirmed, withdrawn to a pulse on overrun, retired on idle. The analysis lives in `RepeatingSourceModel` rather than in a separate `RepetitionAnalyzer`; there was never a second object's worth of work in it.

The row model (#8, #43, #53, #56, in Polish): `LoopRowModel` groups those per-source bars into **one row per inferred loop**, counting iterations rather than records, labelled by the message template, laid out parent-before-child on structural change, and collapsing when quiet. A loop too slow for that row to answer "is it still running?" earns a second, determinate one beneath it — `CyclePositionModel`, reading the body's ordinal straight off the AST. Static AST structure decides the grouping where source is on disk and the timing decides it otherwise, and static also *vetoes* a total that period ordering fabricated across a function boundary. It is rendering-side grouping over what Phase 4 computed — it adds no inference and touches none of the model's frozen state.

What does not exist yet, and must not be described as though it does: `HintsConfig`, the inbound `OTelBridge`, OTel metrics, the DuckDB backend, and multiprocessing-aware capture. Sections below describe the intended design for those. Check before assuming any module named here is on disk.

Everything is pre-1.0 with no released version and no back-compat obligation, so a wrong API shape should be fixed rather than deprecated.

## Vision

**The point, before anything else: see that your program is still working.** Everything below — inference, the display model, the linter, OTel, an eventual MCP surface — is in service of that one question, and when a scope argument cannot be settled any other way, this is what settles it. A feature that does not help someone watch their own long-running job is not obviously worth building. `logging` is the **transport layer**, not the product; it happens to be the one pipe that already exists in every Python program, already carries caller and thread attribution, and already survives concurrency.

`lumberjack` is a drop-in UX layer for Python's stdlib `logging`. The premise: developers scatter `logger.debug(...)` calls through code while building it, then delete or bury them once things work — but a log line that recurs inside a loop is progress signal, not noise. Instead of deleting them, `lumberjack` intercepts them: it captures every record into a queryable store (full fidelity, never lost), while rendering a live progress bar in place of a thousand scrolling lines. Concurrency (threads, processes, asyncio tasks) falls out of the same mechanism — each source's ticks are attributed at write time, so multiple workers become multiple progress bars for free.

**Log density is input quality, and that inverts the usual advice.** The signal lumberjack reads is the *shape of the event stream* — which source locations fire, in what order, how often — so more diagnostic ticks mean better cycle resolution, not more noise. The instinct to delete debug lines once the code works is removing exactly what the display is built from. The pitch is not "we tolerate your log spam"; it is "keep it, and add more."

The intended experience is a value ladder:
- **Drop it in** (`import lumberjack; lumberjack.init()`) — existing log lines become progress display, attributed per worker, no other code changes.
- **Log idiomatically** — a line announcing each stage, a line per item processed, ticks inside the loop rather than around it. **No lumberjack API at all**, and no lumberjack-specific idiom: this is ordinary good logging, the kind that makes a log file useful to read. The display gets dramatically better for it, and this is the rung most codebases have the most to gain from.
- **Instrument a little more** (`lumberjack.track()` / `lumberjack.task()`, or a hints config) — exact progress, named tasks, task hierarchy, where you bother to say so.
- Nothing in between is required. Uninstrumented code still works; instrumented code just looks better.

**The middle rung is the pitch, and it costs nothing but good practice.** "Add logging to your app in an idiomatic way and we turn it into a better UX" asks for no dependency, no API, and no lumberjack-shaped thinking — and the logs it asks for are more useful than a cluttered log even with lumberjack uninstalled. That is what makes the ask reasonable. It also means **the instrumentation linter (#40) is not a nice-to-have diagnostic — it is the mechanism that moves people up the ladder**, because it can say *which* line to add and where, statically, without anyone reading this document. Where the display cannot infer something, the first question is whether idiomatic logging would have supplied it; only when the answer is no does `track()` become the argument.

**Third-party libraries narrate their setup, not their work — do not assume otherwise.** The appealing version of rung 1 is that calling a slow uninstrumented library shows you it is alive, because it logs as it goes. Measured against matplotlib, that is false in the case that matters:

| call | wall time | records | sources |
|---|---|---|---|
| 1st `savefig` (cold font cache) | 0.05s | 93 | 4 (one with 90 hits) |
| 2nd `savefig` (warm) | 0.03s | 0 | 0 |
| 12×200k points, dpi 200 | **2.76s** | **0** | **0** |

The 90-hit source is a real loop and lumberjack bars it correctly — but it is font-cache *initialization*, a one-time startup cost. The 2.76 seconds of actual rendering is silent, and every call after the first is silent. So the honest claim is that libraries log at **setup boundaries** and at **per-item I/O**, not in proportion to work done. Some libraries will be much better (anything doing per-request or per-file work logs per item). The mechanism must not be assumed, and how far rung 1 actually carries on code you did not write is an empirical question nobody has answered — worth a survey of real libraries before any more design leans on it.

### Where the idea came from

Four observations, in the order they arrived. They are recorded because each one still constrains a decision, and because two of them are load-bearing in ways that are easy to undo by accident.

**1. Debug logging is written to be deleted.** You add a line to confirm you hit a branch, or that a slow thing is still moving, and you take it out once it works. Leaving it in is nearly free — but only in the state where it is *filtered out*. Measured (`benchmarks/capture.py`): a `logger.debug` the level discards costs ~100ns against a ~11ns empty loop, which is noise. Once the record is actually built it is ~4.7µs, **roughly fifty times** the filtered call, and lumberjack's whole proposition is that you turn those lines on. So the honest claim is not "logging is free"; it is that leaving the lines in costs nothing until you ask for them, and asking for them costs single-digit microseconds per record — of which stdlib's own record construction is the larger half. The benchmark exists to keep that sentence true.

**2. The cost of keeping them is a big file — and now, a big context.** A verbose log is merely large on disk. What changed is that a wall of log text is actively expensive to feed to an agent, which is the modern version of "nobody reads it". A queryable store answers that in a way a file cannot: ask for the last error, or the rate of one source, instead of pasting ten thousand lines. This is the argument for the MCP surface (#55's neighbour, deliberately unscheduled) and it is also why the store is lossless while the display is lossy — they serve different readers.

**3. `logging` already captures the caller.** The starting idea was to find the calling line number with `sys._getframe` and key loop detection off it. `LogRecord` already carries `pathname`, `lineno` and `funcName`, captured by stdlib and then usually thrown away by every formatter. So identity costs nothing to obtain and is exact — no inference, no message parsing, no fingerprinting. **Everything else in the design rests on this**, and it is why Principle 3 forbids inferring attribution from message text: the accurate answer was already in the record.

**4. Progress bars get hard under concurrency; logging does not.** A single-threaded progress bar is easy. Add threads, processes or asyncio and you are managing `position=`, locks, and which bar belongs to whom — `tqdm` makes the author declare depth at authoring time, which breaks on recursion and on a function that is sometimes nested. Logging does not get harder: it is already thread-safe, and `LogRecord` already carries `thread`, `threadName`, `process` and `processName`. So multi-worker progress falls out of the same mechanism rather than being a feature. This is the strongest form of the pitch and should not be traded away.

**And the linter already exists.** `ruff`'s `G` rules enforce exactly the discipline this design wants, without knowing lumberjack exists. G001–G004 forbid building a log message with `%`, `.format()`, `+` or an f-string — i.e. they require `log.debug("row %d: parsed", i)` over `log.debug(f"row {i}: parsed")`. The consequence matters more than the style:

| call | `record.msg` | `record.args` |
|---|---|---|
| `log.debug("batch %d: validating", i)` | `'batch %d: validating'` | `(5,)` |
| `log.debug(f"batch {i}: validating")` | `'batch 5: validating'` | `()` |

Lazy formatting keeps the **template** and the **data** in separate fields, and the store already writes both (`msg` and `message` are distinct columns). An f-string destroys the template at the call site, unrecoverably. So a G-compliant codebase hands lumberjack a stable human-readable name per source location for free — no parsing of rendered text, ever, which is the thing Principle 3 exists to prevent. `G` is in this repo's own `select` for that reason.

### Static structure is the ground truth inference approximates

The linter (#40) has to parse the code to advise on it. That AST pass produces, as a by-product, most of what the runtime analysis is trying to reconstruct from timing — and produces it exactly. Prototyped against `examples/demo.py`:

```
run_sequence():
  loop@164, nesting depth 1 — 5 call site(s):
     1/5  line 165  'batch %d: opening connection'
     ...
     5/5  line 173  'batch %d: committing'
reconcile():
  loop@132, nesting depth 1 — 1 call site   'reconciling batch %d'
  loop@134, nesting depth 2 — 1 call site   'compared row %d against ledger'
```

That is: sibling grouping (#8), lexical containment (#38's ratio work), **ordinal position within the body** (#53), and the template (#56) — all four, statically, keyed on `file:lineno`, which is already the identity axis. Nothing has to line up; it is the same key. All four are now in use: the first three by `LoopRowModel`, and #53's ordinal position by `CyclePositionModel` on top of it.

Two consequences worth spelling out:

- **#53 got much cheaper, and then got built.** Interleaving analysis was dropped because it needed a per-record sequence read where everything else needed only an aggregate. With the body's order known statically there is no sequence to track at runtime: a record arrives from line 176 and position is a dict lookup — `3 of 5`. The expensive half of the feature evaporated, and what shipped adds no store query of any kind.
- **The `phases` false parent disappears by construction.** The AST does not claim the stage-announcement line encloses the stage functions, because lexically it does not. Period ordering says it does. Static analysis fails *silently* here (it cannot see cross-function containment without a call graph) rather than fabricating a relationship, which is the right failure mode.

**What it does not give**, and why runtime analysis is not replaced: iteration counts, rate, which worker, and whether anything is still running are all runtime facts. Cross-function containment needs a call graph and dynamic dispatch defeats it. Code without source on disk — `exec`, generated modules — has no AST to read. So static structure **seeds** the model; it does not become the model, and everything must still work when it is absent.

This also unifies three things that were separate: the linter advises, the hints config declares, and static analysis measures — but a linter that can already see the structure can **emit** the hints file, so Phase 5's config stops being something a human writes by hand and becomes something generated and then edited. Worth deciding before Phase 5 designs a config format for hand-authoring.

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
- `RecordStore` — pluggable interface (SQLite default/stdlib, DuckDB optional) behind the shared write buffer. Roughly: `append(records)`, `recent(n | since)`, `count_by_template(window)`, `count_by_source(window)`, `count_by_source_since(after_id)`, `task_events_since(after_id)`, `evict(before | keep_last)`, `templates()`. One implementation, two SQL dialects, one parametrized test suite across installed backends. `recent(n)` returns the last n oldest-first, which is also what the exit dump reads — there is deliberately no separate `tail()`. `count_by_source_since()` is the one anything on a timer should call: it groups only rows past a watermark, so a redraw costs what arrived rather than what the store holds.
- Tracking API (`lumberjack.track`, `lumberjack.task`) — explicit progress reporting. When a session exists its events become `logging` records that travel the normal handler→buffer→store path, so the handler stays the only writer; with no session it emits nothing (see Design Principle 4). `task()` mirrors an OTel span; `track()` mirrors `tqdm`. Object model: `task(...)` returns a handle that is itself the context manager (`__enter__` returns `self`); `.subtask()` returns the same type for nesting. `otel.py` is the only module importing `opentelemetry`, guarded, and exposes `tracer()` as a *function* so one monkeypatched seam forces the absent path on every CI job.
- **Repetition analysis** — infers loop structure from the event stream, and lives inside `RepeatingSourceModel` rather than in an object of its own. **Identity is source location, not message text**: a call at `foo.py:6` is the same call on every iteration, exactly, with no inference and no masking. Message content is enrichment; the primary signal is temporal and structural. It reads *ordinary* log records — `task_event` rows are ground truth to be trusted directly, never evidence to infer from, because Phase 2 samples its ticks and inferring a period from a sampled stream produces a fictional one. All of it runs per *poll* rather than per record, over sources rather than rows, so redraw cost is set by how many bars there are and never by log volume.

  Three measurements, in increasing difficulty:

  | Question | From |
  |---|---|
  | How fast is this loop iterating? | inter-arrival times for one source |
  | How many iterations so far? | count for one source |
  | Are two sources the same loop, or nested? | period ordering, scoped to one worker |
  | How long until done? | the ratio between an enclosing period and an enclosed one |

  A source's own recurrence interval *is* its loop's period — no clustering needed to time a loop, only to decide how many bars to draw.

- **Totals, and the ratio that yields them.** Rate says how fast, never how far. Sorting sources by period descending and cutting wherever the period drops by more than a tolerance recovers loop levels directly: equal periods are two log lines in one body, and a much shorter period is a loop running inside a slower one. The ratio between an enclosing level's period and an enclosed one's **is the inner loop's total** — containment and the total come from the same measurement. So a loop pulses until the ratio has held across consecutive polls and is a determinate bar after.

  **Containment is scoped to one worker, and that is load-bearing.** `count_by_source_since()` groups by `(process, thread, asyncio_task_id)` as well as by source and reports which workers ran each line; a parent candidate must share one with its child. Without it, period ordering alone reads *any* two unrelated loops with a stable integer ratio as nested — not hypothetically, but for the three independent worker threads in `examples/demo.py`. The search then runs outward from the child rather than stopping at the level immediately above, because another worker's loop easily lands between a real parent and its child in a list ordered purely by period.

  What was designed and deliberately not built here: interleaving verification (counting how many B's fall between consecutive A's). It needs a per-record sequence read where everything above needs only the aggregate the store already returns, and the worker check closes most of what it would have caught — and #57's static structure supplies the body's order outright, which is what #53 went on to draw from, so the runtime version is now unlikely ever to be written. Merging a 1:1 pair into one row *was* built, but a layer up: it is a display decision (`LoopRowModel`), not more inference, and it waited on #43 settling how rows are placed at all.
- `HintsConfig` — declarative config where the user explains message semantics (progress step, task boundary, noise, severity override); sits above the analyzer, so declared meaning beats inference.
- `OutputModeDetector` — interactive TTY vs plain/structured (file or pipe), with explicit override.
- `Renderer` — abstract interface, all implementations optional-dependency guarded and falling back to plain when `rich` is absent or stderr is not a TTY. `RichProgressRenderer` is the live display and the one that matters: it owns the `Live`, draws named bars above inferred loop rows, and is what `init()` selects on a TTY. It exposes both layers deliberately — `bars()` is one entry per source location in records, `rows()` is one per loop in iterations. A row may carry a `position`, which the renderer draws as a second `Task` under it. `PlainTextRenderer` covers plain text and JSON-lines, write-through and not timer-gated. `RichTerminalRenderer` prints one styled line per record and is reachable only by constructing it directly — `create_renderer()` returns it when there is no store to read, and `init()` always has one. Live TTY redraw is periodic (~200ms), timer-driven, decoupled from log volume; non-TTY output is always write-through. The live display leaves `sys.stdout` alone (rich would redirect it to *stderr*, silently moving a program's own output) and routes `sys.stderr` through the console so a raw write prints above the bars instead of corrupting the frame.
- `RepeatingSourceModel` — accumulates `count_by_source_since()` forward from a watermark, and is where the repetition analysis above lives. Cumulative counts are **monotonic by construction**: `evict()` can drop the rows a bar counted without the bar counting backwards, which is what a progress bar has to mean. The *cycle* position a nested bar fills to is the one number that resets, and deliberately — it describes one iteration of the enclosing loop, not the run, so the two are separate fields. One entry per source location, unbounded on purpose: that is the identity layer, and 300 repeating log sites are 300 things that were captured.
- `LoopRowModel` — the **display** layer over that, and the distinction is the whole point (see below). One row per inferred *loop*, counting iterations, labelled by `record.msg`'s template, ordered by containment then by liveness, collapsed when quiet. It is grouping over `BarState`, not new inference, and it deliberately reaches into none of the model's frozen state — `_shown`, `_parent`, `_total`, `_cycle_base` and the monotonic counts are what make eviction safe, and making them loop-scoped would trade that guarantee for a label. Its supporting parts: `layout.depth_first_order()` (structural order), `templates.TemplateIndex` (`record.msg` per source, harvested from a bounded tail read, which is also what feeds `static.template_matches()`), `position.CyclePositionModel` (the second row, below), and `static.py` for the structure itself.
- `OTelBridge` — optional `SpanProcessor`/`MetricReader` feeding the same store (inbound direction; outbound is `task()` emitting spans directly).

### What the display is for

Everything above describes what can be *inferred*. This describes what the display is *for*, which is a separate question and was never written down — so display choices got made bottom-up, by whatever the inference happened to produce, rather than by what a person needs to see. Several of them were defaults nobody chose. **Mostly built now: the row model (#8, #43, #56) and #53's intra-iteration row shipped; the heartbeat shipped under #54, whose counter element and #55 are what remain.** Paragraphs below say which is which.

**The criterion: a row earns its place by updating at a rate a human can read.** A loop that ticks once every three minutes is a *correct* bar and a *useless* one — it cannot distinguish a running program from a hung one, which is the first question the display exists to answer. When the best available row is too coarse to be legible, the display should find a finer signal inside it or say nothing. This is the principle underneath the bar-count, bar-placement and sequence questions, which have been circling each other as three separate display problems and are one.

**Display unit ≠ identity unit, and conflating them was the root mistake.** *Built.* Source location is the right *identity* key — exact, cheap, no inference — and nothing here moves off it. But it became the *display* unit by default, because grouping already produced it. A person does not want a bar per call site; they want the shape of their program. So sibling call sites in one loop body merge into **one row per inferred loop**, named for the loop, with source locations as the identity underneath. `LoopRowModel` is that grouping: rendering-side, over what `RepeatingSourceModel` already computes, and adding no inference of its own.

**Static structure decides the grouping, and the timing is the fallback.** *Built.* `static.py` states which call sites share an innermost loop and which loop encloses which, exactly, keyed on `file:lineno` — the identity axis already in use, so nothing has to be reconciled. Every claim from it is gated on `template_matches()`, because a file edited since the running process imported it makes `file:lineno` point somewhere else and every structural claim keyed on it wrong. Where there is no source on disk — `exec`, a generated module — grouping falls back to the runtime signal: equal periods and a shared worker. Both halves are needed; neither is sufficient.

**A loop is up to three rows, and the criterion decides how many.** This is where the criterion stops being a slogan. One merged loop can render as:

1. **the loop** — a pulse, or determinate if a total is genuinely known; counts **iterations**, not records. *Built.*
2. **position within the current iteration** — determinate, ticked by the body's call sites in order (#53). *Built.*
3. **the most recent message**, optionally, as text. *Not built as a per-row element; the session heartbeat carries one for the whole session.*

Row 2 appears *only when row 1 is too slow to be legible*. That is the whole rule, and it resolves what look like two contradictory answers. `siblings` — four call sites in a loop running at 120/s — is **one** row reading `400`, because a sub-iteration bar at that rate is a blur nobody can read. `sequence` — five call sites in a loop taking 1.5s per iteration — is **two** rows, because the outer bar alone ticks too rarely to distinguish running from hung, and the position within the iteration is the only legible signal available. Same structure, same merge, different number of rows, decided by measured period rather than by taste. (Two rather than three because row 3 was deliberately not built. "Up to three" is the shape; two is what a loop earns today.)

**Row 2 is read, not measured, and that is what made it cheap.** `static.CallSite.position` and `body_size` are the ordinal of a call site within its innermost loop body and the size of that body, both frozen by the AST walk — so a record arrives from line 171 and the position is a dict lookup. Which stage is *current* comes from `BarState.last_at`, the newest timestamp per source, which is already in `count_by_source_since()`'s aggregate. **No per-record read was added, and none may be**: that cost is exactly why interleaving analysis was refused twice, and the whole argument for building this now is that static structure removed it. The line is drawn by `RichProgressRenderer` as a second rich `Task` keyed on the *loop row's* key, threaded into the re-layout immediately after it — a position row has no identity of its own and no place in the containment order.

Three refusals, and each is a refusal rather than a guess. A body with a branch has no stable order (`Loop.stable_order`), so `transform`'s conditional warning costs it the row — a determinate bar over it would show a *wrong* percentage, which Principle 10 does not license. A body with one call site is `1 of 1` forever, which fails the criterion that admitted it; `phases`' announcement line is that case. And no source on disk, or a template that failed the drift guard, leaves no ordinal to look up. In all three the loop row draws exactly as it did before.

**`MIN_LEGIBLE_PERIOD` is 1.0s and admission is one-way.** The number is bounded on both sides by things that are measured rather than felt: a redraw is every 200ms, so a loop at the threshold moves the row above once every five frames — about the slowest tick that still reads as motion — while `siblings` at 8ms an iteration would repaint the position bar from whichever of ~24 iterations the poll landed in. Not 2s, which would refuse `sequence`, the case it was built for; not 0.5s, where the loop row already answers the liveness question. And once a loop has earned the row it keeps it, because a period wobbling across the threshold would otherwise make a row appear and disappear — the same reason bars never move and containment never re-parents.

The count on row 1 is **iterations of the merged loop**, not records captured. `siblings` reads `400`, not the 1600 records behind it — and this is not a cost to be accepted, which is how it was first written down here. It is the correction.

The call sites say `row %d: parsed`, `row %d: validated`, and so on. The domain object is the **row**, there are 400 of them, and that is what the code is making progress through. That four log lines happen to fire per row is an artifact of how the author chose to narrate it; nobody asked how many `log.debug` calls occurred. So 1600 was the accidental number all along, and per-source counting only looked authoritative because it matched the store. Record count is an **identity-layer** number — the right answer to "what did we capture" and the wrong answer to "how far along is this". The store, the exit summary and `RichProgressRenderer.bars()` remain where the former is asked.

Where merged call sites fire *unequal* numbers of times — a conditional error line inside the body — "iterations" is ambiguous, and the answer is the highest count among the merged sources: a line that fires every iteration is a better clock than one that fires sometimes. Max of monotone counts is monotone, so the eviction-safety invariant survives the merge unchanged.

**A row is named for what it does, not where it lives.** *Built (#56).* Under lazy `%`-formatting stdlib keeps the template and the data in separate attributes, and the store already writes both — so `record.msg` is a constant per source location and a human-readable description of the line. `reconciling batch …` beats `demo.py:132 reconcile()`. This is reading a structured field, the same category of act as reading `lineno`; it is *not* the message-content inference Principle 3 forbids, and the distinction is worth keeping sharp. An f-string at the call site destroys the template, at which point nothing matches and the row falls back to `file:line func()`. A merged row has several templates and no reason to prefer one, so it takes the loop's location instead.

**Inference gets the shape; instrumentation gets the numbers — and the gap is a feature.** Two of the hardest questions here have the same answer, and it is not cleverer inference. An outer loop's total is "ideally but unlikely" to be inferable, so it stays a pulse until someone wraps the range in `track()`. Distinguishing "A encloses B" from "A precedes B" — the false parent in `phases` — is not solvable from logs at all, and `track()` settles it directly. What static analysis adds is a **veto** rather than an answer: where the AST shows the child is a top-level loop in another function or file, the claimed parent cannot lexically enclose it, so the fabricated total is withheld and the row pulses. The indent stays, because the stage function really is called from inside that loop and what static cannot see is a call graph. So the display shows what it knows, claims nothing more, and lets the shortfall be visible: a stopped heartbeat or a permanent pulse is **a prompt to log more or to instrument**, which is the value ladder working as intended rather than a failure to paper over. Ask them in that order — the middle rung above is cheaper for the user than the top one and closes most gaps, so "you have no line inside this loop" is a better first answer than "wrap it in `track()`". The instrumentation linter (#40) is what says which, statically.

**The screen budget is real whether or not it is acknowledged.** *Built.* Rich crops at terminal height regardless, so "unbounded" never meant everything is shown — it meant the cropping rule was *whoever qualified first*, the one rule with no argument behind it. The budget is now filled deliberately: whole subtrees with something still running first, then quiet ones ordered by how recently they moved. Quiet rows **collapse** — a mark instead of forty columns of finished bar — which cannot save vertical space and is not trying to: a retired row stays in place, because deleting it would empty the final frame that "drain before closing" exists to preserve. What collapses is its width and its weight.

**Four element types, because forcing everything into a bar makes things lie.** A determinate bar (a total is known), a pulse (active, claiming nothing), a **counter** (this happened N times — no rate, no progress, the honest form for a source that fires once), and a session **heartbeat** (overall liveness from total arrival rate across all sources; needs no new signal, the store already has it). The pulse-vs-determinate distinction currently rides entirely on colour and animation, which is one fragile channel carrying the whole uncertainty vocabulary.

**The heartbeat stops when the records stop, and says nothing else.** No "idle" label, no elapsed counter, no spinner turning on wall-clock — those all claim liveness nobody observed. When matplotlib spends 2.76 silent seconds rendering, a stopped heartbeat is the truthful frame. It reads as "we cannot see anything", which is exactly right, and the fix belongs to the developer rather than the display.

**What this does not license.** None of it is a reason to move off source-location identity, to infer from message text, or to let the display invent structure it has not measured. A row that claims less is always available and always allowed.

### Decisions worth not relitigating

- **A high bar count is a symptom of unmerged identities, and the fix is merging, not capping.** *Settled and built.* 800 bars meant the display was drawing one row per call site. Capping *that* — collapsing the excess into a neutral "… 298 more" row — hides the symptom and fixes nothing, which is why it was refused and stays refused. 800 identities now render as a handful of loop rows, so the count stopped being a display problem without any signal being suppressed, and the budget allocates what remains by relevance rather than by arrival order. `LUMBERJACK_MAX_BARS` remains a terminal-compat escape hatch — opt-in, absent from the README, reported once at exit. The equivalent question for *named* task bars, which cannot be merged, is #68.
- **Two bar kinds share one `Live`; they are never two started `Progress` objects.** `Progress.start()` starts a `Live` of its own, and rich allows only one live per console: the second becomes `_nested`, at which point its `refresh()` re-renders *the root's* renderable and returns, so its bars never draw (verified against rich 15.0.0, `Live.refresh`). Giving each its own `Console` is worse — two Lives writing cursor control to one stderr corrupt the frame. The working shape is rich's documented one: the renderer owns a single `Live(Group(heartbeat, task_progress, source_progress))` and **neither `Progress` is started**. Owning the `Live` is also where issue #28's bounded final frame belongs.

  **Group order is by what survives cropping, and the rule generalised when the heartbeat arrived.** The original form was "exact bars first, because the ellipsis crops bottom-up and instrumented bars must not lose their slots to inferred ones" — right, but it was really a claim about *precedence under a height budget* rather than about exactness. The heartbeat now sits above both, because it is the row that answers "is anything happening at all", which is the first question the display exists to answer and the only row a program with no repeating log lines draws. So the order is: heartbeat, then exact bars, then inferred ones — most-certain-and-most-load-bearing first, and a cropped frame loses the guesses rather than the facts.
- **Rich crops rather than corrupts.** `Progress` runs `Live` with `vertical_overflow="ellipsis"`, so an over-tall live frame shows the first N bars plus an ellipsis with correct cursor arithmetic. Do not justify display work by claiming otherwise. `Live.stop()` does *not* crop, though — see issue #28.
- **At exit, drain before closing the display.** A live bar draws its closing frame from the store, so `teardown.run()` flushes the buffer first; closing first leaves the final count short, or with the pump disabled draws no bar at all. The excepthook path is the opposite by design — there a traceback is imminent, so the display comes down first.
- **Pulse means "no claim", and confidence only ever increases.** `add_task(total=None)` gives rich an indeterminate, pulsing bar, and that one mechanism carries all the uncertainty the display needs: pulsing before a cycle is detected (work is happening, nothing more is claimed), determinate once a total is known, and **pulsing again if the count exceeds the estimate** — degrading to honesty rather than showing 127% or freezing at 100%. Promotion is one-way, for the same reason bars never move: a display that oscillates between spinner and bar as confidence wobbles is worse than one that stays a spinner.
- **A bar retires on idle relative to its own period,** not on any completion signal — there isn't one. Absence of events is ambiguous (a slow iteration and a finished loop look identical), because nothing raises `StopIteration` at a log line. Roughly 10× the measured interval with no event is the 80% answer: wrong for a loop with wildly varying iterations, right nearly always, and cheap. Do not hold out for a correct answer here; there is no signal that would provide one. Three details the implementation forced. The threshold is floored at a second, because a millisecond loop's 10× threshold is shorter than the buffer-flush plus redraw latency feeding the model, and it would retire and resurrect on alternate frames. A merged row is idle only when *every* member is, because a body whose last line is conditional must not retire the loop between one iteration's first line and the next's. And "retires" means *marked idle in place* — the row fills, collapses to a mark, the rate column reads `idle`, and it stays: deleting it would empty the final frame that "drain before closing" exists to preserve, and would say the work stopped existing rather than stopped.
- **Nesting depth is derived, never declared.** `tqdm` makes the author pass `position=` and manage `leave=` by hand, so depth is static and known at authoring time — which breaks on recursion, on a function that is sometimes top-level and sometimes inside a loop, and with threads. Deriving depth from observed containment removes the bookkeeping *and* covers the cases tqdm structurally cannot, because depth becomes a property of what happened rather than something declared in advance. This is the strongest form of the pitch and should not be traded away for implementation convenience.
- **Rows move on structural change, never on counts.** *Settled and built.* The original rule was "bars never move once placed", justified by a bar that jumps around as counts overtake each other being unreadable. That justification is sound and is kept — but it was over-applied. It bans reordering by a *continuously changing* metric; it does not follow that structure may never be re-laid-out. Inferred containment changes rarely and is frozen once confirmed, so moving a row when the *structure* changes is stable in practice, while refusing to move it was wrong permanently.

  This was not an abstract cost. In `examples/demo.py` the model correctly infers that `reconcile:134` (190/s) nests inside `reconcile:132` (10/s) — ratio 19.7, drawn `20/20` — and used to render it indented beneath **`extract:100`**, an unrelated loop on another thread, because that is where it first qualified. An inner loop always qualifies before its outer one, so this was the norm and not an edge case. The single visual cue for hierarchy was reporting a false hierarchy, which is worse than reporting none: it is the display asserting something untrue, and Principle 10's "the display may be wrong" licenses an imprecise bar, not a fabricated relationship.

  `rich.progress.Progress` renders tasks in `add_task()` order, so placement is decided at registration. `Progress.tasks` is `list(self._tasks.values())` over a plain dict, so **insertion order is render order** and re-laying-out means rebuilding that dict under the progress's own lock — `_relayout()`, private access for the same reason `_set_total` has it. **Reorder, never remove and re-add**: recreating a `Task` throws away its elapsed clock and its completion. Two tests pin rich's half of that so an upgrade fails loudly rather than silently scrambling the display. The order itself (`layout.depth_first_order`) reads no count, rate or percentage, which is what makes recomputing it every poll safe.

  The same reasoning is why a loop row's key is frozen at first sighting and never migrates as members join it: the key is what the `Task` is filed under.
- **A `TaskHandle` parents only inside a `with`,** and out-of-order resets are handled rather than prevented. Only `__enter__` sets the ambient contextvar and only `__exit__` clears it, so a handle used bare holds no token and cannot be the one whose reset misfires. That is *not* enough to make out-of-order resets impossible: `with` is LIFO only within one frame, two suspended generators each holding one interleave freely, and `ContextVar.reset()` does **not** raise for an out-of-order token from the same Context — it silently writes the old value back. So the ambient slot is never trusted directly. `_unbind()` resets only while still the current binding, and `_ambient_parent()` walks past any handle that has already ended, following where each was *entered* rather than where it was created. Both guards are load-bearing and separately mutation-tested; a bug was found here twice.
- **`.subtask()` takes its parent as `self`,** never from ambient context, because contextvars propagate into asyncio tasks but **not** into a bare `threading.Thread`. It must also build the child's OTel context from `self._span`, or the log and span hierarchies disagree in exactly that threaded case.
- **`GeneratorExit` is not a task failure.** It is what a user's generator receives when the consumer stops early, so a plain `break` must not put a traceback on the ERROR channel — the one line a user is guaranteed to see — or mark the span failed. `track()`'s own early `break` already records a clean end; a bare `with` inside a generator has to agree.
- **Progress ticks are sampled; the `end` row is not.** `advance()` emits at most one record per 50ms. The unconditional reason is that non-TTY output is write-through, one line per record, so per-item ticks print a million lines for a million-item loop — the disease the package exists to cure, caused by the cure. A second reason is real but machine-dependent and should not be quoted as a law: an unsampled loop can emit faster than the pump drains, so fast hardware overflows lumberjack's own buffer and trips its own dropped-records warning while slower hardware does not. **Do not quote a number for this from memory — run `benchmarks/capture.py`.** Three different figures have been written down here (85,000/s, then 59,000/s, then 85,000/s again on a third box) and each contradicted the last, which is why the benchmark exists and why this sentence no longer carries one. What is durable is the *shape*: a write-through renderer costs per record and a live bar does not, so plain and JSON are several times more expensive per call than rich, and the buffer→store drain is a ceiling above which records are evicted rather than the loop being slowed. Nothing is lost either way, because `progress_current` is **absolute**, not a delta, and `end()` writes the final count unsampled. Do not "fix" a bar that looks coarse by lowering the interval.
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

Two SQLite specifics that measurement forced, both commented at their call sites. `count_by_source_since()` uses `NOT INDEXED`: left alone SQLite serves the `GROUP BY` from `idx_records_source` as a covering index, scanning every row to answer a delta query, and ruling the index out turns it into a rowid range seek (32ms → 0.5ms at 1M rows). `evict(keep_last)` resolves a cutoff id and deletes by range rather than `id NOT IN (SELECT …)`. A second backend will need its own answers to both — these are not portable. Worth knowing alongside them: `count_by_source_since()` also groups by `(process, thread, asyncio_task_id)`, which containment analysis needs and which costs a wider sort key and one group per (source, worker) pair — 5.2ms against 3.3ms at a 5,000-row delta over 1M rows with 20 sources across 8 threads.

### Integration surfaces

`task`/`track` mirror existing concepts on purpose rather than inventing vocabulary: `task()` ↔ an OTel span, `track()` ↔ `tqdm`. Libraries calling these impose no dependency and no runtime cost on downstream users — what they become (a real progress bar, a real OTel span, both, or nothing at all) is decided entirely by what the *application* installs and whether it calls `init()`. Known incompatibility until interception lands (post-1.0): `tqdm` writes its own ANSI/cursor control and will fight a live lumberjack display if both run concurrently.

## Phased plan (high level)

Full detail lives in the project plan; phase order is deliberate (simplest-first, exact-before-inferred):

0. **Done.** Foundations — repo scaffolding, packaging, CI, pre-commit, test harness before implementation.
1. **Done.** MVP — capture/store/render skeleton, including a throwaway crude end-to-end proof (one hardcoded/naively-detected repeating log shape rendered as a live bar) to validate the core premise early.
2. **Done.** Tracking API (`track`/`task`), outbound OTel only — establishes the progress/task model before inference is built on top of it.
3. Multiprocessing-aware capture — prototype SQLite WAL multi-writer before building an IPC/queue fallback. **Deferred behind Phase 4 — see execution order below.**
4. **Done.** Repetition analysis & inferred progress — the phase that delivers the core premise (recurring log line → progress tick). Structure first (rate, count, containment, nested bars); message-text analysis is a later refinement, not its basis. Ran in two halves, exact before inferred:
   - **4a.** Named determinate bars from stored tracking data, no inference at all. `TaskProgressModel` folds `store.task_events_since()` into one bar per task; the renderer draws them above the source bars in one shared `Live`.
   - **4b** (issue #38): per-source rate and count, then containment from period ordering scoped to one worker, pulse→promote, idle retirement. Reuses 4a's display model rather than inventing one. Interleaving verification and 1:1 bar merging were designed and deliberately left out — see the analysis section above.
5. Hints config.
6. OpenTelemetry integration, inbound bridge (spans/metrics from other instrumented libraries). **Re-evaluate before starting — see below.**
7. Polish & extensibility (renderer interface finalized, theming, performance pass, docs).
8. (Post-1.0) Progress provider interception — monkeypatch `tqdm.tqdm` at `init()`.
9. (Stretch) Web renderer & disk-backed persistence.
10. (Stretch) Profiling & contingent Rust migration for a specific bottleneck, gated on profiling data — not scheduled work.

### Where OTel actually sits, revisited

Phase 2 was "tracking API, outbound OTel only", and in hindsight the OTel half was early. Worth separating the two, because they are not the same bet:

- **The tracking API (`task`/`track`) was right and is load-bearing.** It is rung 3, and it is what settles a total or a parent/child relationship that inference cannot. Most of Phase 2 was this.
- **Outbound spans deliver nothing to lumberjack's own display.** They emit *to* an OTel backend. Real interop value for someone already running OTel; zero value to the question at the top of this document. Being shipped and inert is fine — it is small and guarded — but it should not have been sequenced ahead of anything the display needed.
- **The inbound bridge is the half with nesting information**, and that is the argument for keeping Phase 6: parent/child span relationships from an already-instrumented third-party library are ground truth about containment that nothing else can supply. If a library emits spans, we learn its structure without its source and without inference.

But rank the sources of containment ground truth by coverage before scheduling it:

| Source | Exact? | Coverage | Status |
|---|---|---|---|
| Period-ratio inference | no | any code that logs | built |
| `task()` / `.subtask()` | yes | code you chose to instrument | built |
| Static AST | yes (lexical only) | any code with source on disk | not designed |
| Inbound OTel spans | yes | code already OTel-instrumented | Phase 6, unbuilt |

Inbound OTel is exact but has the narrowest reach of the three exact sources, and it is the only one requiring a dependency and a running collector. Static analysis covers far more code for less. So the honest sequencing is that **Phase 6 is interop, not the nesting fix** — it should be justified by "lumberjack works in an OTel shop", which is a real reason, rather than by the structure it happens to carry.

### Phases are GitHub milestones

Phases 0–7 exist as milestones. **The milestone number is the phase number plus one** (Phase 0 is milestone 1, Phase 7 "Polish & extensibility" is milestone 8) — the API takes the number, not the title, so assign one issue and read it back before batching. Phases 8–10 have no milestone yet; nothing maps to them.

File new work against its phase. Defects in already-shipped code and anything genuinely ambiguous go to **Polish & extensibility** — correcting minor defects is what polish means — rather than being left unassigned or forced into a phase they do not belong to.

**Execution order is not phase order.** Phase 4 was built before Phase 3. Nothing in Phase 4 touches the write path — it adds *readers* — while Phase 3 changes how records get into the store, so there is no collision and no rework. Multiprocessing capture is also infrastructure whose payoff is *more bars*, which is worth little until the bars themselves are worth multiplying. The numbers and milestones are deliberately left as they are: renumbering would invalidate "Phase 4" in every issue body that already cites it, for no gain. **The SQLite WAL multi-writer question is a one-day spike, not a phase** — if WAL genuinely supports multiple writer processes, most of Phase 3 evaporates, so run the spike opportunistically and record the answer as a milestone comment.

### What each milestone is, and is not

Written down so "does this belong here?" stops being a judgement call.

| Phase | IN | OUT |
|---|---|---|
| **3 — Multiprocessing capture** | Records from child processes reaching the parent's store (WAL multi-writer if the spike says yes, otherwise a queue path). The attribution columns already exist. | Any display change. DuckDB multi-process — the queue path is its answer by design. Anything networked. |
| **4 — Repetition analysis** *(done, closed)* | What shipped: 4a's named determinate bars from stored tracking data, then rate/count, worker-scoped containment ratios, pulse→promote, idle retirement (#38), and #26. | Everything the milestone once also claimed — see below. Message-template masking. Hints. Caller fingerprinting. Multiprocessing. |
| **5 — Hints config** | Declarative config that overrides inference: progress step, task boundary, noise, severity. | New inference of any kind. Runtime API surface beyond reading the config. |
| **6 — OpenTelemetry** | The inbound `OTelBridge` (SpanProcessor/MetricReader → store), the `trace_id`/`span_id` columns (#34), and tagging ordinary records with their enclosing task (#32) — #34 and #32 are the same capture-path cost question and are decided together. | Outbound spans — Phase 2 shipped them. Exporter or sampling configuration. Rendering metrics beyond the existing bars. |
| **7 — Polish & extensibility** | Defects in already-shipped code (#7, #11, #14, #15, #16, #18, #31, #33, #35), the row model (#8, #43, #53, #56 — **done**; #68 remains), #37's detection-plus-diagnostic, organisational cleanup (#44, #45), renderer interface freeze, theming, performance pass, docs. | New inference. Caller fingerprinting (#37's later options). Anything owned by an earlier phase. |

**Phase 4 closed without five things it had claimed, and they were re-homed rather than left to rot.** Worth recording, because the pattern will recur: a milestone accumulates issues while the phase is being designed, and shipping the phase is when you find out which of them were actually part of it.

| Issue | Was | Now | Why |
|---|---|---|---|
| #8 | Phase 4 | Polish | The bar-count ceiling is a *display* question, and Phase 4 changed what a bar means without settling it. Retirement bounds what claims to be live; it does not bound what is drawn. Closed in Polish, by merging rather than capping. |
| #43 | Phase 4 | Polish | Filed *by* Phase 4 and blocked on #8 — same question, so the same home, and closed with it. |
| #35 | Phase 4 | Polish | A Phase 2 defect that Phase 4 made visible rather than caused: a stalled task genuinely does sit at 1/100 now that named determinate bars exist. |
| #37 | Phase 4 | Polish | The cheap half (detect the collapse, report it at exit) is polish-shaped. The expensive half — caller fingerprinting — stays out of every milestone. |
| #32 | Phase 4 | OpenTelemetry | Not a defect at all, and never really Phase 4's: it is a capture-path cost question, and #34 says outright that the two must be decided together. |

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
uv run mypy --strict src && uv run mypy tests examples benchmarks
uv run pytest
```

### Benchmarking

`benchmarks/capture.py` answers the question the pitch depends on: what does it cost to leave the debug logging in? Seven arms — no logging, a `logger.debug` the level discards, stdlib to a `NullHandler`, stdlib to a file, then lumberjack in `plain` / `json` / `rich` — so the package is measured against the alternatives a developer actually has rather than against zero.

Five things about its construction are deliberate and should survive edits:

- **Two numbers per arm, because either alone lies.** *In-loop* is time inside the logging call, which is what the calling thread feels. *Total* adds the drain, forced with a final `flush()`. lumberjack defers the drain to a background thread, so in-loop understates the true cost and total overstates the felt one.
- **The buffer is sized to the run.** At the default 10,000 a fast loop outruns the pump, the buffer evicts, and the row measures how quickly lumberjack discards a record — a different question, and one the separate `measure_drain()` answers properly. A row that lost records is not a measurement, so it is annotated and the script exits non-zero.
- **Minimum of repeats, not mean, and the raw repeats are kept beside it.** The noise is one-sided — every source of it adds time and none subtracts — so the distribution is right-skewed and its lower envelope is the most reproducible feature. Measured across repeated runs the minimum was as stable as or better than the median in 12 of 14 arm×metric cells; mean±stddev is worse on both counts, since the mean tracks box load and the standard deviation assumes a symmetry the data lacks. The per-repeat values are retained anyway, because the minimum alone cannot say whether a delta cleared the noise, and because a later comparison can then compute a statistic this version did not think of without invalidating baselines already on disk.
- **`stored` is a correctness check, not a statistic.** `dropped == 0` only says the buffer never evicted; comparing `stored` against records-plus-warmup says they reached the store, which is Principle 6's actual promise. Both checks run over **every** repeat, not the fastest one — an earlier version carried the fastest repeat's notes while reporting the worst repeat's drop count, so a set where only a slow repeat overflowed printed `dropped=500` and still exited zero. `tests/test_benchmark.py` drives that aggregation with stub samples, because no end-to-end run catches it.
- **The floor arm declares itself with `Arm.emits`,** rather than the setup function's name being sniffed. Renaming `_no_logging` under the old scheme silently demoted the floor to a second filtered-out arm — every "vs floor" ratio shrinking about tenfold — and nothing asserted otherwise, since the corrupted floor was still the cheapest row.

The drain figure it prints is the **batched best case** — one `flush()` of the whole run against the pump's many small timer-driven batches. It is an upper bound, not a rate to plan against.

#### What the instrument can and cannot resolve

Measured on a 4-vCPU shared Xeon, and the reason `--repeats` defaults to 5 rather than 3:

| | |
|---|---|
| Within one process, 10 repeats | MAD 0.5–2.4% of median; spread 2.5–11.3%; **every** outlier high, none low |
| Reported minimum, run-to-run, `--repeats 3` | 7–13% |
| Reported minimum, run-to-run, `--repeats 5`, quiet box | 1.3–5.6% |
| Ratio against the floor, run-to-run | 8–12% |
| `measure_drain()`, run-to-run | 13.7% |

So **a sub-5% change is invisible to a single before/after pair** on this class of machine. That is a property of the instrument, and `--compare` says so rather than implying precision it does not have: verdicts are `noise` unless the two runs' repeat *ranges* are disjoint, and `suspect` when they are disjoint but the delta is under 5%. Range-disjointness rather than a t-test because five samples of a skewed one-sided distribution do not meet a parametric test's assumptions, and a reader can check non-overlap by eye against the spreads printed beside it.

**Per-record cost is run-length dependent, so a baseline is only valid at its own `--records`.** The write-through modes measure ~25µs/record at 10k and ~44µs at 200k; the stdlib arms and rich are flat. The cause is the pump regime — a 10k run finishes in ~0.24s, barely one 200ms tick, so it never pays steady-state contention. The default of 100k sits in the steady state deliberately. It is *not* store growth across repeats: each `_measure()` re-runs `init()`/`shutdown()` against a fresh `:memory:` store, and later repeats measured slightly faster, so the min-of-repeats estimator is not biased by it.

**Ratios are what travels between machines; absolutes are what to diff on one.** This inverts for comparison and the distinction matters: run-to-run on one box the ratios are *less* stable than the absolutes they are built from, because the denominator is 12ns of pure loop overhead carrying its own noise. Quote ratios in prose — see the sampling note above for three mutually contradictory absolute figures each written down as fact — and let `--compare` read absolutes.

**Baselines are gitignored (`benchmarks/*.local.json`), never committed,** and `--compare` withholds verdicts when the record count, repeat count or machine fingerprint differs. Committing one would recreate precisely the failure the sampling note records. `tests/test_benchmark.py` also runs the script end to end at 200 records on every suite run, asserting only that it executes and loses nothing — never on timings, which at that size are noise and would flap on a busy CI runner. Making it a CI *gate* needs a dedicated runner, which does not exist; that decision lives in its own issue rather than being relitigated here.

### CI

Jobs are independent — knowing *which* is broken beats making one wait on another, the same reasoning behind `fail-fast: false` on the matrix.

| Job | Guards |
|---|---|
| `lint` | Once, not six times — style is a property of the source, not the interpreter |
| `typecheck` | `mypy`, strict on the shipped package |
| `test` | 6 legs: {ubuntu, windows} × {3.12, 3.13, 3.14} |
| `bare install (no rich)` | Principle 8 — the zero-dependency install. Asserts `rich` is genuinely absent so it cannot rot into a duplicate of `test` |
| `package` | `uv lock --check`, builds the wheel, installs it into a clean venv, asserts `py.typed` ships |
| `coverage` | One suite run with `--cov-report=xml`, uploaded to Codacy, which renders the README badge from it |

CodeQL and Codacy also run, both configured outside this workflow.

A separate workflow, `demo-gif.yml`, re-records the demo gif on every PR
that touches `src/`, `examples/` or the recorder, and uploads it as an
artifact. It is a smoke test as much as a regeneration: it is the one check
that runs the whole stack — `init()`, worker threads, the live display,
teardown — under a real pty, so it failing while the suite is green flags
something untested. On a push to `daddy` (or a manual dispatch) a second
`publish` job also force-pushes the gif as a single orphan commit to the
`demo-assets` branch, which the README's image URL points at — the same
shape as the coverage badge: trunk is never pushed to from CI, the asset is
served from somewhere CI *can* write, and the gif is no longer committed to
the repo at all (`docs/demo-*.gif` is gitignored for local recordings).
`publish` is the one job anywhere with `contents: write`, scoped to that
job, and a PR run never reaches it. Neither job is a required check.

### Releasing

`.github/workflows/release.yml`, separate from CI because it runs on different events and needs a permission CI must never have.

**Publishing is one-shot per version number.** PyPI refuses a re-upload of a version even after you delete it, so every mistake costs a number. Everything about the workflow's shape follows from that:

- **`workflow_dispatch` defaults to TestPyPI.** It is the rehearsal, and the only way to see how the long description renders and whether the metadata is right *before* spending a version. Use it first, every time.
- **Publishing a GitHub Release publishes to PyPI.** That is the real one.
- **The tag must match `pyproject.toml`.** Tagging `v0.1.0` while the file still says `0.1.0.dev0` would publish a dev release under a number nobody meant to spend, so the build fails instead. Bump the version and the `CHANGELOG.md` heading in the same commit as the tag.
- **`twine check --strict` runs before anything is uploaded.** A README that fails to render leaves a permanently ugly project page. This nearly happened once already: the coverage badge used to be a *relative* path, which renders on GitHub and nowhere else.
- **The wheel is built once and verified, then that same artifact is published.** Rebuilding between the check and the upload would mean publishing something nothing tested.

**Trusted Publishing, not an API token.** GitHub mints a short-lived OIDC token that the index exchanges for upload rights, so no long-lived credential exists to leak — the same reasoning that keeps CI on a read-only workflow token. It binds to the repository, the workflow *filename*, and the GitHub environment name, so all three are configured on the index side: renaming `release.yml` or the `pypi` / `testpypi` environments breaks publishing until the publisher is updated to match.

The `pypi` environment is also where a required reviewer belongs, if publishing should ever need a second pair of eyes.

The workflow is also `workflow_dispatch`-able, which is not a convenience. A commit message can suppress a run outright — GitHub scans the entire message for a skip directive, body included, so *quoting* one in prose is enough — and a suppressed run is never created, so there is nothing to re-run afterwards. Branch protection then refuses the empty commit that would force one. The result is a trunk commit with no verdict at all, which is indistinguishable from a green one at a glance. Manual dispatch is the way back.

Coverage is uploaded to Codacy from the `coverage` job. It comes from that one ubuntu/3.12 run rather than all six `test` legs: the union across legs would be marginally higher — the bare-install `skipif`, Windows path branches — but collecting it means six `--partial` uploads plus a `final` call, which is a lot of workflow for a fraction of a percent. The upload step is skipped, not failed, when `CODACY_PROJECT_TOKEN` is absent, which is the case for fork pull requests and for anyone who cloned this without a Codacy project — and the skip emits a workflow notice saying so, because a silent skip is indistinguishable from a broken upload when the job goes green either way.

**No job writes to the repository, and the README's coverage badge is Codacy's rather than a committed SVG.** It used to be one: the job rendered `coverage.svg` with `genbadge` and pushed it to trunk under `[skip ci]`. Branch protection ended that — a direct push cannot satisfy status checks that only run *after* a push, so every trunk push failed on that step while all eleven real checks passed. A permanently red trunk that means nothing is worse than no signal. Serving the badge from the coverage already being uploaded removes the push, the `contents: write` escalation, the `[skip ci]` dance, and a generated file the repo had to keep in sync with itself. The whole workflow now runs on a read-only token. The one deliberate exception lives outside CI: `demo-gif.yml`'s `publish` job holds `contents: write` to force-push the recorded gif to the `demo-assets` branch — a branch with no code, no protection and no checks, so the failure mode that killed the badge push (a trunk push that cannot satisfy its own required checks) structurally cannot recur there.

**One job name is deliberately wrong.** `bare install (no rich)` guards three optional dependencies, not one. `name:` is the check name GitHub reports and trunk's branch protection lists this job, so renaming it orphans a required check and blocks merges until the ruleset is edited to match — worth doing only alongside that settings change. The `coverage` job used to have the same problem under its old name `coverage-badge`; it was renamed freely because the ruleset does *not* list it.

**Trunk's required checks are `lint`, `typecheck`, the six `test` legs, `bare install (no rich)`, `package`, and CodeQL's `Analyze (actions)` / `Analyze (python)` — twelve.** `package` was added after this list was first written down, and it earns its place: it is the only check exercising the artefact users actually install, building the wheel, installing it into a clean venv and asserting `py.typed` ships. `coverage` is the one check that runs and is deliberately not required — a coverage upload failing, or being skipped on a fork PR with no token, is not a reason to block a merge.

**Bandit's ruleset lives in ruff, and `.codacy.yaml` has no bandit block.** It used to: 437 of bandit's 447 findings were `assert` used in a pytest suite, where the assert *is* the test, so `tests/**` was excluded wholesale — and every *other* finding bandit would have reported there went with it. What anyone actually meant was "an assert is fine in tests/ and nowhere else", and that is unsayable in `.codacy.yaml`, whose own comment records why: individual patterns can only be turned off in Codacy's web UI, so path scoping is all the file can do.

`ruff --select S` reproduces what bandit reports exactly — rule for rule and site for site, checked rather than assumed — and ruff *does* have `per-file-ignores`. So `pyproject.toml` now says `"tests/**" = ["S101", "SLF001"]` and nothing else is exempt anywhere. This is strictly stronger than the old arrangement: an `assert` reaching `src/` used to pass `lint` and be caught by Codacy only after merge; it now fails pre-commit and the required `lint` job. Remaining findings are suppressed inline with the reason beside them, and a line needing both tools carries both markers — bandit reads `# nosec` and has never read `# noqa`.

`bandit.yaml` at the repo root carries `skips: [B101]`, which is what actually silences the assert rule for Codacy's Bandit engine — ruff's `per-file-ignores` governs ruff only, and Codacy's Bandit reads neither it nor `# noqa`. `skips` rather than `exclude_dirs: [tests]` deliberately: excluding the directory would restore the blind spot this change removed, where B603/B108/B102 in `tests/` went unreported. Bandit auto-discovers **no** config file — not `pyproject.toml`, not `.bandit`, both tested — it reads one only when passed `-c`, which Codacy does for the filenames it recognises when a tool's "use configuration file" setting is on. It is already on for Bandit and Ruff here, so the file needed no UI change; renaming it silently disables it.

Where a security rule is right, it gets fixed rather than muted: the seven `try`/`except`/`pass` swallows in `teardown.py`, `pump.py` and `rich_renderer.py` are now `contextlib.suppress(Exception)`, identical in behaviour and flagged by nothing, which also lifted `teardown.py`'s coverage from 84% to 91% by deleting seven unreachable `pass` lines. Only two findings are suppressed, both in `store.py` and both argued inline — fixing either would mean duplicating the schema `_COLUMNS` exists to keep single, or restructuring queries whose plans were measured.

Two rules ride along because they were already being suppressed by hand, which makes them either real or decoration: `SLF001` and `BLE001`. `RUF100` is what stops any of it rotting — it found 15 dead `# noqa: E402` directives the day it was switched on. `SLF001` is exempt in `tests/` deliberately: 31 of its findings are tests asserting on renderer internals, which is issue #45, and suppressing them inline would bury the thing #45 exists to fix.

`opengrep` and `pylintpython3` still exclude `tests/`, `benchmarks/` and `scripts/` — different findings, not addressed by any of this.

**Every action is pinned to a commit SHA, with its version in a trailing comment.** A git tag is moveable: whoever owns the action can repoint `v4` at different code and every run picks it up with no diff and no review. That matters here because CI holds a Codacy token. Bump with `git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<tag>^{}'` — the `^{}` dereferences an annotated tag to the commit. The pins are currently unmanaged, so they will go stale; issue #42 holds the Renovate-or-Dependabot decision that would fix that.

Every job pins its interpreter with `actions/setup-python` *before* `setup-uv`. Without it `uv sync` resolves whatever satisfies `requires-python` and the matrix silently stops testing six versions — verified from run logs that the legs really do run distinct interpreters.

`.pre-commit-config.yaml` pins `ruff`/`black` by git rev while `uv` resolves them from `pyproject.toml`; nothing links the two, so `tests/test_toolchain.py` fails when they drift. Otherwise a commit passes locally and fails `lint` over a rule one version has and the other does not.

### Environment variables

| Variable | Effect |
|---|---|
| `LUMBERJACK_OUTPUT_MODE` | `rich` / `plain` / `json`, overriding TTY detection |
| `LUMBERJACK_MAX_BARS` | Opt-in ceiling on drawn bars. Debug/compat aid, deliberately undocumented in the README — see the decisions above |

Both follow the same rule: a bad **argument** is a caller's bug and raises; a bad **environment variable** is an operator typo, so it warns and degrades.

### Verifying display behaviour

`examples/demo.py` carries one scenario per shape of log stream (`--list` describes them, `--all` runs every one); several are shapes the display handles badly and are there as design fixtures. Bars only render on a TTY, so piping the demo shows the plain renderer instead. To see the real thing, run it under a pty and strip ANSI. Note `examples/demo.py` calls `shutdown()`, which unregisters the `atexit` hook — a script that exits naturally is needed to observe exit-time diagnostics.
