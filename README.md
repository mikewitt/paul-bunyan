# lumberjack

[![CI](https://github.com/mikewitt/paul-bunyan/actions/workflows/ci.yml/badge.svg?branch=daddy)](https://github.com/mikewitt/paul-bunyan/actions/workflows/ci.yml)
[![Coverage](coverage.svg)](https://github.com/mikewitt/paul-bunyan/actions/workflows/ci.yml)

A drop-in UX layer for Python's stdlib `logging`. Capture every log record at
full fidelity into a queryable store, while rendering something concise —
instead of a thousand scrolling `DEBUG` lines, render progress.

**Status: early scaffolding.** Capture, storage (SQLite), output-mode
detection, plain/rich rendering, and a first live progress bar are in place.
The bar's repetition detection is deliberately crude for now — records are
grouped by *source location*, so a log call inside a loop becomes one bar.
Template-based repetition analysis and the explicit `track`/`task` API are
later phases and do not exist yet — see `CLAUDE.md` for the full plan.

## Install

```bash
pip install lumberjack               # zero required dependencies (library use)
pip install lumberjack[recommended]  # + rich, for interactive terminal output (application use)
```

## What use looks like

Three rungs, and you stop at the one you need. The first is the normal case;
the other two are opt-in.

### 1. Drop it in

```python
import logging
import lumberjack

log = logging.getLogger(__name__)


def main():
    lumberjack.init(level=logging.DEBUG)

    for i in range(10_000):
        log.debug("processed item %d", i)   # this line becomes one bar


if __name__ == "__main__":
    main()
```

That is the whole thing. No context manager, no `try`/`finally`, no cleanup
call — see [Do I have to shut it down?](#do-i-have-to-shut-it-down) below.
`init()` returns the installed handler; ignoring the return value is normal.

`level` sets both the root logger's level and the handler's, and defaults to
`INFO`. lumberjack exists for `logger.debug()` spam, so pass
`level=logging.DEBUG` when that is what you want captured — otherwise stdlib
`logging` filters it out before lumberjack ever sees it.

On an interactive terminal with `rich` installed, log lines that repeat from
the same place stop scrolling and become bars that advance. Three worker
threads, each logging inside its own loop (`examples/demo.py`), render as:

```text
demo.py:43 extract()   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 700 records 0:00:02
demo.py:49 transform() ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 450 records 0:00:02
demo.py:60 load()      ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 300 records 0:00:02
```

One bar per source location — three loops, three bars, no concurrency-specific
setup. (Grouping is by source location, not by worker: two threads running the
same loop share a bar today, though thread and process are still recorded on
every record.) The count climbs but there is no percentage, because nothing
here knows how many iterations are still coming; exact totals are what the
`track()` / `task()` API will add in a later phase.

Nothing is lost to the collapse:

- every record is in the store, queryable — that is [rung 2](#2-read-back-what-was-captured);
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
— [rung 3](#3-manage-the-lifecycle-if-you-need-to).

### 2. Read back what was captured

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

records = store.recent()            # everything, oldest first
print(len(records))                 # 1000
print(records[-1].message)          # processed item 999
print(records[-1].thread_name)      # MainThread
print(records[-1].func_name, records[-1].lineno)
```

`recent()` takes `n` (the last n, still oldest first) or `since` (an epoch
timestamp); with neither, it returns the lot. Each record carries stdlib
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

### 3. Manage the lifecycle, if you need to

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
| `init(**options)` | Always. For most applications it is the only call. Returns the handler. |
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
| `level` | `logging.INFO` | Level for the root logger and the handler. `logging.DEBUG` to capture debug spam. |
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
uv run mypy tests examples
```
