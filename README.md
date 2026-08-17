# lumberjack

[![CI](https://github.com/mikewitt/paul-bunyan/actions/workflows/ci.yml/badge.svg?branch=daddy)](https://github.com/mikewitt/paul-bunyan/actions/workflows/ci.yml)
[![Coverage](https://app.codacy.com/project/badge/Coverage/cb0069a163c44c1ea01a4e86c7a16fb9)](https://app.codacy.com/gh/mikewitt/paul-bunyan/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_coverage)
[![Code quality](https://app.codacy.com/project/badge/Grade/cb0069a163c44c1ea01a4e86c7a16fb9)](https://app.codacy.com/gh/mikewitt/paul-bunyan/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)

A drop-in UX layer for Python's stdlib `logging`. **The question it exists to
answer is "is my program still working?"** — so it captures every log record
at full fidelity into a queryable store, and renders something a person can
actually read: instead of a thousand scrolling `DEBUG` lines, progress.

Logging is the transport, not the product. It is the one pipe every Python
program already has, it already records which line emitted each record and on
which thread, and unlike a progress bar it does not get harder when you add
concurrency.

![Four worker threads logging inside their own loops, rendered as live progress bars](docs/demo-pipeline.gif)

Four threads, ~2,000 `logger.debug` calls, and **no lumberjack API anywhere in
the worker functions** — that is `examples/demo.py`, unmodified, under
`lumberjack.init()`. Recreate it with `uv run python examples/demo.py`.

**Status: early, pre-1.0, and not published yet.** Working today: capture,
SQLite storage, output-mode detection, plain/JSON/rich rendering, the explicit
`track()` / `task()` API with outbound OpenTelemetry spans and the named bars
it drives, structural inference over uninstrumented logging, one row per
inferred loop with template labels, a session heartbeat, static analysis of a
source file's loop structure, and an instrumentation linter that says which
log line to add and where (`python -m lumberjack.lint`).

Still to come: sub-iteration progress for a slow loop body, a hints config,
the inbound OpenTelemetry bridge, and multiprocessing-aware capture.
`CLAUDE.md` has the design and the reasoning; `examples/demo.py` has it in
runnable form, one scenario per shape of log stream.

## Install

**Not on PyPI yet** — the name is not settled, so install from source:

```bash
git clone https://github.com/mikewitt/paul-bunyan
cd paul-bunyan
uv sync --all-extras        # or: pip install -e ".[recommended]"
```

Once published there will be two shapes, and the split is deliberate: the base
install pulls in **nothing**, so a library can instrument without imposing a
dependency on anyone downstream, while `[recommended]` adds `rich`, which *is*
the interactive display. Every optional dependency degrades rather than errors
— no `rich` means the plain renderer, not a crash.

## What use looks like

Five things you can do, in rough order of how much you have to say. The first
two are the normal case; the rest are opt-in, and plenty of programs never
need them.

### 1. Drop it in

```python
import logging
import lumberjack

log = logging.getLogger(__name__)


def main():
    lumberjack.init()

    for i in range(10_000):
        log.debug("processed item %d", i)   # this line becomes one bar


if __name__ == "__main__":
    main()
```

That is the whole thing. No context manager, no `try`/`finally`, no cleanup
call — see [Do I have to shut it down?](#do-i-have-to-shut-it-down) below.
`init()` returns the installed handler; ignoring the return value is normal.

`level` sets both the root logger's level and the handler's, and defaults to
`DEBUG` — deliberately louder than stdlib's usual default. lumberjack exists
for `logger.debug()` spam, and any higher default has stdlib discard those
calls before lumberjack ever sees them, so a first run would show nothing.
The volume is handled where it belongs: the display collapses it and the
store absorbs it. Pass `level=logging.INFO` for a quieter capture.

On an interactive terminal with `rich` installed, log lines that repeat from
the same place stop scrolling and become bars that advance. Four worker
threads, each logging inside its own loop (`examples/demo.py`), render as:

```text
⠴  1,863 events · 565.2/s     fetched row 671 from source table
fetched row … from source table ━━━━━━━━━━━━━ 672 iterations         233/s 0:00:02
normalized record …             ━━━━━━━━━━━━━ 405 iterations         140/s 0:00:02
wrote batch … to warehouse      ━━━━━━━━━━━━━ 281 iterations         97/s  0:00:02
reconciling batch …             ━━━━━━━━━━━━━ 24 iterations          9/s   0:00:02
  compared row … against ledger ━━━━━━━━━━━━━ 19/20 · 480 iterations 188/s 0:00:02
```

The top row is the **heartbeat**: how many records have arrived, how fast, and
the newest line. It answers "is anything happening at all" before any loop has
been identified, and it is the only row a program with no repeating log lines
will draw. It moves when records arrive and **stops when they stop** — never on
a timer, so a frozen heartbeat means the program has genuinely gone quiet
rather than that the animation ran out. That is a prompt to log more, and it is
honest: when a library spends three silent seconds inside a C extension, there
is nothing to see and saying otherwise would be a lie.

Below it, **one row per loop** — not per log line. Several call sites in one
loop body collapse into a single row, because a person wants the shape of
their program rather than a bar per call site. Source location stays the
identity underneath; it is simply not the display unit.

Two consequences worth naming. Rows are labelled by the **message template**,
which stdlib keeps separate from the rendered text whenever the call uses lazy
`%` formatting — so `log.debug("fetched row %d from source table", i)` names
its own row, with nothing parsed out of the output. And the count is
**iterations**, not records: the fourth row says `24`, and the `reconcile`
pair below it did 480 comparisons across those 24 batches.

Three of those loops are flat, so their bars only count and pace: nothing in
the stream says how long they are, and claiming otherwise would be a guess.
`reconcile` runs a loop inside a loop, and *that* is in the stream — the inner
line fires twenty times between consecutive firings of the outer one — so it
draws indented under its real parent with a `20/20` nobody declared. When a
loop goes quiet its row collapses to a marker and reads `idle`, rather than
vanishing: a finished run should still show what it did.

One thing this deliberately does not do: it does not group by worker. Two
threads running the same loop share a row, though thread and process are
recorded on every record and *are* what stop two unrelated loops being read as
nested.

Inference is an 80% solution on purpose, and it will be wrong sometimes. When
it is, the cost is a cosmetic one: a bar that pulses when it could have had a
percentage, or one that overshoots and goes back to pulsing. The store is
never wrong. [Telling it outright](#3-tell-it-what-the-work-is) is what
`track()` and `task()` are for.

Nothing is lost to the collapse:

- every record is in the store, queryable — see [reading back what was captured](#4-read-back-what-was-captured);
- `WARNING` and above still prints above the bars, because the one line you
  actually needed to see must not be hidden by the thing that hides noise;
- when the display is the lossy kind, the last 50 records are replayed at exit,
  so a collapsed run still ends with its tail on screen.

Piped to a file or another program, output is plain, one line per record,
write-through, and free of ANSI and cursor control. lumberjack writes to
stderr and decides by looking at it: a progress bar redrawing itself into a
log file is corruption, not output.

Run both against the same program to see the difference:

```bash
uv run python examples/demo.py                                # bars, on a terminal
LUMBERJACK_OUTPUT_MODE=plain uv run python examples/demo.py   # the scrolling they replace
```

The worker functions are identical between those two runs and know nothing
about lumberjack. That is the point: the display is a property of how the
application was configured, not of how the code was written.

The demo carries other shapes of log stream too — a slow loop narrating its
own stages, one loop with several call sites in its body, startup lines that
never repeat, a wrapper that collapses every call site onto one:

```bash
uv run python examples/demo.py --list       # every shape, and what it exercises
uv run python examples/demo.py sequence     # one of them on its own
```

Several are shapes lumberjack currently handles badly, and they are in there
for that reason — each states what it logs, what the display does with it
today, and what it *should* do, with the open questions marked. They are the
working material for the display design, not a feature tour.

#### `init()` is for applications, never libraries

It takes exclusive ownership of the root logger — replacing its handlers and
setting its level. Pass `replace_handlers=False` to layer alongside whatever
is already installed instead. A library should do what libraries have always
done: attach a `logging.NullHandler()` and let the application decide.

#### Do I have to shut it down?

**No.** A program that runs and exits needs no cleanup call from you. `init()`
registers an `atexit` hook and takes over `sys.excepthook`, and those do the
work on the way out:

1. drain the write buffer into the store, so records logged in the last
   moments still land — and so a live bar's closing frame shows the count the
   run actually finished on;
2. stop the live display, restoring the cursor;
3. print a warning if the bounded write buffer ever overflowed, naming how
   many records never reached the store;
4. replay the last `dump_last_n` records (default 50) if the display was a
   lossy one.

On an unhandled exception the display comes down first instead, before
Python's excepthook prints — a traceback must never be overwritten by a
redraw or mangled by leftover cursor control — and the drain still happens
on the way out. The background flush thread is a daemon, so it never delays
interpreter shutdown either.

`shutdown()` is for the cases where process exit is *not* the end of the story
— see [managing the lifecycle](#5-manage-the-lifecycle-if-you-need-to).

### 2. Log the way you would anyway

This is the step that matters most and asks for the least: **no lumberjack API
at all**, just log lines placed where they were always most useful. The display
gets dramatically better for them, and the log file is better to read even with
lumberjack uninstalled — which is what makes it a reasonable thing to ask.

- **Leave the `logger.debug` lines in, and add more.** Density is input
  quality. A loop that logs once per iteration is a bar; a loop that logs
  nothing is invisible, and no amount of inference recovers it.
- **Log inside the body, not around it.** A line before and after a loop says
  it started and finished. A line *in* it says how fast it is going.
- **Narrate a slow body.** Five lines inside a three-second iteration can say
  where you are within it; one line can only say that it happened.
- **Announce each stage of a multi-stage routine**, one `log.info` apiece.
- **Use lazy `%` formatting, never f-strings**, in log calls. `log.debug("row
  %d", i)` keeps the template and the data in separate fields, so the template
  can label a row; `log.debug(f"row {i}")` destroys it at the call site.
  `ruff`'s `G001`–`G004` enforce exactly this, and are worth enabling
  regardless of lumberjack.
- **Never write a logging wrapper without `stacklevel=2`.** Identity is the
  source location, so a shim makes every call site in your program look like
  one line.

`examples/demo.py` carries a scenario per shape of log stream, including the
ones lumberjack currently handles badly, and states for each what it does today
versus what it should. An instrumentation linter that reports which of these a
codebase is missing is planned
([#40](https://github.com/mikewitt/paul-bunyan/issues/40)) — the point being
that a tool can name the specific line to add, which prose cannot.

### 3. Tell it what the work is

Inference gets what it can from how often a log line repeats, which is a great
deal for a nested loop and nothing at all for an outermost one — no amount of
watching a top-level loop reveals how many iterations are left. `task()` and
`track()` are how code says so outright, and a stated total always beats an
inferred one.

```python
import lumberjack

for doc in lumberjack.track(docs, name="reindex"):
    index(doc)

# or, when you are not iterating anything:
with lumberjack.task("migrate", total=len(tables)) as t:
    for table in tables:
        migrate(table)
        t.advance()
```

`track()` mirrors `tqdm`; `task()` mirrors an OpenTelemetry span. Tasks nest,
and `t.subtask("name")` parents a child explicitly — which is what you want
off the main thread, since context does not propagate into a bare
`threading.Thread`:

```python
with lumberjack.task("etl run") as run:
    threading.Thread(target=worker, args=(run,)).start()

def worker(parent):
    with parent.subtask("extract", total=700) as t:
        ...
        t.advance()
```

**This works in libraries, and imposes nothing on their users.** Unlike
`init()`, the tracking API has no import-time or call-time dependency on
anything being configured. What your `task()` call *becomes* is decided
entirely by the application that runs your code:

| The application has | Your `task()` produces |
| --- | --- |
| lumberjack installed, nothing configured | nothing at all — no output, no log line |
| OpenTelemetry configured the normal way | OTel spans |
| called `lumberjack.init()` | records in the store, and the display |
| both | both |

So a library can instrument freely: no imposed dependency, and no lines
printed into a host application's logs unless that application asked for
them.

Two things worth knowing:

- **Progress ticks are sampled** — roughly one record per 50ms, not one per
  `advance()`. Piped to a file, output is write-through: a record per item
  would print a line per item, which is the thing this package exists to
  avoid. Counts stay exact regardless, because the value is absolute and the
  closing record carries the final one.
- **These draw as real bars.** A task with a total shows a percentage; one
  without pulses rather than inventing a denominator; subtasks are indented
  under their parent, and a bar finishes when its task ends. Uninstrumented
  log lines still get their inferred count bars, drawn below these.

`examples/tracking.py` is the whole thing end to end.

### 4. Read back what was captured

The display is lossy on purpose. The store is not, and `current_store()` is
the supported way in.

```python
import logging
import lumberjack

lumberjack.init()

for i in range(1000):
    logging.info("processed item %d", i)

lumberjack.flush()                  # drain the buffer into the store right now
store = lumberjack.current_store()
assert store is not None            # None before init() and after shutdown()

records = store.recent()            # the last 1000, oldest first
print(len(records))                 # 1000
print(records[-1].message)          # processed item 999
print(records[-1].thread_name)      # MainThread
print(records[-1].func_name, records[-1].lineno)
```

`recent()` takes `n` (the last n, still oldest first) or `since` (an epoch
timestamp), and the two compose as "the last `n` of those at or after
`since`". `n` defaults to 1000 rather than to everything — at the million-record
retention target the unbounded form is a multi-second call that builds half a
million objects, which is a surprising bill for something that looks free.
Pass `n=None` when you do want the lot. Each record carries stdlib
`LogRecord`'s attributes — `message`, `level_name`, `level_no`, `logger_name`,
`pathname`, `filename`, `func_name`, `lineno`, `created`, `exc_text` — plus
the attribution lumberjack captures at write time: `thread_name`,
`process_name`, `asyncio_task_name` / `asyncio_task_id`, and columns held for
the task hierarchy and template clustering that later phases fill in.

The `flush()` is only needed because the read happens immediately after the
writes. A background pump drains the buffer into the store every 200ms, so in
a real run the store is already near-current; `flush()` just removes the race.

`RecordStore` carries more than `recent()` — aggregate counts, eviction — and
`examples/demo.py` uses a couple of them. Treat the rest as unstable for now:
it is the interface the renderers are still being built against.

### 5. Manage the lifecycle, if you need to

`shutdown()` reverses `init()`: it stops the pump, flushes the buffer, tears
down the live display, puts back the root logger's handlers *and* its level,
and closes the store if lumberjack was the one that created it. Reach for it
when the process outlives the run:

- **Tests.** A clean store per test, and the root logger handed back intact
  afterwards.
- **Notebooks and REPLs.** The interpreter does not exit between cells, and
  `init()` raises `RuntimeError` if lumberjack is already installed — so
  re-running a cell means `shutdown()` first.
- **Code that borrows the root logger for one phase** — a CLI subcommand, a
  plugin, a long-running harness — and has to hand the application's logging
  configuration back exactly as it found it.
- **A run that outlives its display.** Pass your own store and `shutdown()`
  leaves it open: the bars stop, the records stay queryable.

```python
import logging
import lumberjack
from lumberjack.store import SQLiteRecordStore

store = SQLiteRecordStore(":memory:")   # or a path, to outlive the process
lumberjack.init(store=store)

logging.info("work happened")

lumberjack.shutdown()                   # display gone, root logger restored
print(len(store.recent()))              # 1 — a store you passed in stays open
store.close()
```

The same shape as a pytest fixture:

```python
import pytest
import lumberjack


@pytest.fixture
def records():
    """Capture this test's log records; hand the root logger back after."""
    lumberjack.init(output_mode="plain")
    try:
        yield lumberjack.current_store()
    finally:
        lumberjack.shutdown()


def test_the_worker_reports_progress(records):
    do_some_work()
    lumberjack.flush()
    assert any("finished" in r.message for r in records.recent())
```

## The public API, and when you would reach for it

| Function | When |
| --- | --- |
| `init(**options)` | Applications only. For most of them it is the only call. Returns the handler. |
| `track(iterable, name=...)` | Wrapping a loop you are already writing. Works in libraries, with or without `init()`. |
| `task(name, total=...)` | Naming a unit of work that is not a loop, or one with subtasks. Same everywhere rules. |
| `current_store()` | To query captured records — `current_store().recent()`. |
| `flush()` | To drain the write buffer into the store now rather than waiting up to one pump interval. |
| `shutdown()` | Only when the process outlives the run: tests, notebooks, code handing the root logger back. |
| `is_initialized()` | Setup code that has to cope with either state — fixtures, notebook cells. |
| `current_handler()` | Narrow: reading `.dropped`, or draining the buffer yourself. |
| `current_renderer()` | Narrow: introspection and tests — which renderer actually got chosen. |
| `current_output_mode()` | Narrow: introspection and tests — mostly answering "why am I not getting bars?". |

Every `current_*` accessor returns `None` before `init()` and after
`shutdown()`. That is deliberate: a type checker makes you say why you know
better.

## `init()` options

| Option | Default | What it does |
| --- | --- | --- |
| `level` | `logging.DEBUG` | Level for the root logger and the handler. `logging.INFO` for a quieter capture. |
| `output_mode` | `None` (detect) | Force `"rich"`, `"plain"` or `"json"`. |
| `replace_handlers` | `True` | Take over the root logger's handlers. `False` layers alongside them. |
| `store` | `None` | Bring your own `RecordStore`. One you pass in is yours — `shutdown()` leaves it open. |
| `buffer_size` | `10_000` | Records the write buffer holds between drains. Overflow evicts the oldest, counts them, and says so at exit. |
| `flush_interval` | `0.2` | Seconds between buffer→store drains. `0` disables the pump, leaving `flush()` and exit to do it. |
| `dump_last_n` | `50` | Records replayed at exit when the display was lossy. `0` disables the dump. |

## Output modes

| Mode | Chosen when | Output |
| --- | --- | --- |
| `rich` | stderr is a TTY **and** `rich` is installed | Live progress bars, redrawn about five times a second |
| `plain` | anything else — pipes, files, CI, no `rich` | One text line per record, write-through |
| `json` | only when asked for | One JSON object per record, write-through |

Precedence: the `output_mode=` argument, then the `LUMBERJACK_OUTPUT_MODE`
environment variable, then the TTY check. The two overrides fail differently
on purpose — a bad argument is a bug in your program and raises `ValueError`,
while a bad environment variable is a typo by whoever launched the process, so
it warns and falls back to plain rather than taking the application down.

Optional dependencies degrade rather than error: with no `rich` installed,
`output_mode="rich"` quietly gives you the plain renderer.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run black --check .
uv run mypy --strict src
uv run mypy tests examples benchmarks
```

### Recording the demo

`scripts/record_demo.py` turns any demo scenario into an animated GIF — the
README's hero image is `pipeline`, recorded this way:

```bash
uv run --with pillow --with pyte --with fonttools \
    python scripts/record_demo.py pipeline
```

It replays the pty output through a real terminal emulator rather than
stripping ANSI, because a live display is a sequence of *edits to a screen*
and stripping the escapes yields every intermediate line ever printed — which
is not what anyone saw.

### What it costs to leave the logging in

`benchmarks/capture.py` measures the capture path against the alternatives a
developer actually has — no logging at all, a `logger.debug` the level throws
away, stdlib to a `NullHandler`, stdlib to a file, then lumberjack in each of
its three output modes.

```bash
uv run python benchmarks/capture.py                    # the table
uv run python benchmarks/capture.py --records 200000 --json
```

**Quote the ratios, not the nanoseconds.** Absolutes move with the machine and
the interpreter build; the relationship between the arms is what survives the
trip to someone else's box.

To track a change across features, record a baseline and diff against it:

```bash
uv run python benchmarks/capture.py --json > benchmarks/baseline.local.json
uv run python benchmarks/capture.py --compare benchmarks/baseline.local.json
```

Comparison works on the **absolute** per-arm numbers, not the ratios — on one
machine the ratios have a tiny, noisy denominator and travel worse than what
they are built from. Baselines are gitignored, and `--compare` withholds its
verdicts unless the record count, the repeat count and the machine fingerprint
all match, so a diff can never quietly span two boxes.

Two limits, both measured rather than assumed: the instrument cannot resolve a
change below about **5%**, and per-record cost is run-length dependent below
~50k records, so a baseline is only comparable at the `--records` it was taken
at. Two runs of unchanged code should read `noise` on every row.

The script exits non-zero if any arm lost records, which makes it a check as
well as a report — and `tests/test_benchmark.py` runs it at a tiny record count
on every suite run, so it cannot rot against an API change between the times
somebody looks at the numbers.
