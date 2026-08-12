# lumberjack

A drop-in UX layer for Python's stdlib `logging`. Capture every log record at
full fidelity into a queryable store, while rendering something concise —
instead of a thousand scrolling `DEBUG` lines, render progress.

**Status: early scaffolding.** Capture, storage (SQLite), output-mode
detection, plain/rich rendering, and a first live progress bar are in place.
The bar's repetition detection is deliberately crude for now — records are
grouped by source location, so a log call inside a loop becomes one bar.
Template-based repetition analysis and the explicit `track`/`task` API land in
later phases — see `CLAUDE.md` for the full project plan.

## Install

```bash
pip install lumberjack               # zero required dependencies (library use)
pip install lumberjack[recommended]  # + rich, for interactive terminal output (application use)
```

## Quickstart

```python
import logging
import lumberjack

lumberjack.init()
logging.info("hello")
```

`init()` takes ownership of the root logger's handlers by default. Pass
`replace_handlers=False` to layer alongside existing handlers instead.

On an interactive terminal (with `rich` installed), a log line that repeats
from the same place — say a `logger.debug(...)` inside a loop — stops
scrolling and becomes a bar that advances:

```text
worker.py:42 process()  ━━━━━━━━━━━━━━━━━━━━━━━  1408 records 0:00:12
```

Nothing is lost: every record is still in the store, `WARNING` and above still
print above the bars, and the tail of the buffer is replayed at exit. Piped to
a file or another program, output stays plain and write-through — no bars, no
cursor control.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run black --check .
```
