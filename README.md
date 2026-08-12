# lumberjack

A drop-in UX layer for Python's stdlib `logging`. Capture every log record at
full fidelity into a queryable store, while rendering something concise —
instead of a thousand scrolling `DEBUG` lines, render progress.

**Status: early scaffolding.** Capture, storage (SQLite), output-mode
detection, and plain/rich rendering are in place. Repetition-based progress
detection and the explicit `track`/`task` API land in later phases — see
`CLAUDE.md` for the full project plan.

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

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run black --check .
```
