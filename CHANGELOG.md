# Changelog

Notable changes, written for people who *use* lumberjack. Internal
restructuring, test work and CI plumbing are deliberately left out — the git
log has those, and a changelog that lists them stops being readable.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html),
with the usual pre-1.0 caveat: **while the major version is 0, a minor bump
may break the API.** lumberjack is pre-1.0 with no back-compat obligation, so
a wrong API shape gets fixed rather than deprecated.

New entries go under `Unreleased`. The release workflow checks that the git
tag matches the version in `pyproject.toml`, so bump both together.

## [Unreleased]

### Added

- **`lumberjack.init()`** — installs a handler on the root logger, picks a
  renderer for the detected output mode, and starts the buffer→store pump.
  `shutdown()` puts the root logger back as it was found, handlers and level
  both.
- **A live progress display on an interactive terminal** (needs `rich`).
  Repeating log lines stop scrolling and become **one row per loop** — not per
  call site: several lines in one loop body collapse into a single row, since
  what a person wants is the shape of their program. Rows count *iterations*
  rather than records, are labelled by the message template that lazy
  `%`-formatting keeps intact, and collapse to a marker reading `idle` when a
  loop falls quiet rather than vanishing.
- **A session heartbeat.** Arrival rate across every source, plus the newest
  line — the row that answers "is anything happening at all" before any loop
  is identified, and the only row a program with no repeating log lines draws.
  It **stops when the records stop**, never animating on a timer, so a frozen
  heartbeat means the program genuinely went quiet.
- **Inferred loop structure.** Sources are timed, sorted by period into loop
  levels, and the ratio between an enclosing level and an enclosed one is
  taken as the inner loop's iteration count — so a loop nested inside another
  draws indented, with a real percentage nobody declared. Scoped per worker,
  so two unrelated loops on two threads are not mistaken for one nested pair.
  A bar pulses until it has something to claim, and goes back to pulsing if
  the count outruns the estimate. Where the source file is readable, its own
  loop structure corroborates all of this — and vetoes a total that period
  ordering fabricated across a function boundary.
- **A second row for a loop too slow to read.** A loop taking a second or more
  per iteration also draws its position *within* the current iteration, taken
  from the body's order in the source. A fast loop does not: a sub-iteration
  bar at 100 iterations a second is a blur, and a row has to update at a rate
  a human can read to earn its place.
- **An instrumentation linter** — `python -m lumberjack.lint`. Reports which
  log line to add and where: a loop with nothing inside it, a slow body with
  one line, a wrapper missing `stacklevel=`, an f-string that destroyed its
  template. It recommends ordinary logging before it recommends `track()`,
  and `--agent-rules` prints a block for `CLAUDE.md`/`AGENTS.md` so coding
  agents stop deleting the debug lines the display is built from.
- **`lumberjack.task()` and `lumberjack.track()`** — explicit progress, for
  where inference is not enough. `track()` mirrors `tqdm`; `task()` mirrors an
  OpenTelemetry span and nests via `.subtask()`. Both are **inert without
  `init()`**, so a library can instrument with them and impose neither a
  dependency nor output on the applications that use it.
- **Outbound OpenTelemetry spans** from `task()`, when OTel is configured.
  Independent of whether `init()` has run.
- **A queryable record store.** Everything captured is kept, not just what the
  display shows: `current_store().recent()`, plus aggregate counts by source
  and by template. SQLite, from the standard library, so the base install
  needs nothing.
- **Plain-text and JSON-lines output** for pipes and files, chosen
  automatically when stderr is not a terminal, and never emitting ANSI or
  cursor control. Timestamps are timezone-aware ISO 8601; JSON lines carry
  both the epoch float and the ISO string.
- **Clean teardown on exit and on an unhandled exception**, with the display
  brought down before a traceback prints, and a replay of the last records so
  a collapsed run still ends with its tail on screen.
- **Type hints throughout**, with `py.typed` shipped in the wheel.

### Defaults worth knowing

These are choices rather than accidents, and each is the one most likely to
surprise someone:

- **`init()` captures at `DEBUG`**, not stdlib's `WARNING` or a tidier `INFO`.
  `logger.debug(...)` inside a loop is exactly what lumberjack turns into
  progress, and any higher default has stdlib discard those calls before
  lumberjack sees them. Pass `level=logging.INFO` for a quieter capture.
- **`init()` replaces the root logger's handlers.** Pass
  `replace_handlers=False` to layer alongside them instead.
- **`store.recent()` returns the last 1000 records.** Pass `n=None` for
  everything — at the retention target that is a multi-second call.
- **Progress ticks are sampled** at one record per 50ms. Counts stay exact
  regardless, because the value is absolute and the closing record carries the
  final one.

### Known limitations

- **Multiprocessing is not supported yet.** Records from child processes do
  not reach the parent's store.
- **A logging wrapper collapses inference.** Identity is the source location,
  so a shim that calls `logger.debug()` on behalf of the whole program makes
  every call site look like one. `stacklevel=` in the wrapper fixes it.
- **A loop body with a conditional log line gets no position row.** Its order
  is not reliable, and a wrong percentage is worse than none.
- **`tqdm` and lumberjack fight over the terminal** if both are live.
