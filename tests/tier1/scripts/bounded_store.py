"""A run long enough to outgrow its store, and what is left in it.

The lossless claim has a companion nobody had written down. `loop_to_bar.py`
pins that every record reaches the store; this pins that the store does not
then grow for ever, which is what a long-running job would otherwise do —
and a long-running job is the case this package exists for.

`retain` is the smallest value `init()` accepts. Below it a burst can be
evicted between two redraws and a bar under-counts, so the floor is where
that stops being cheap; a real application takes the default of a million.

The periodic `flush()` is not ceremony. The write buffer is bounded and
evicts under pressure, so a loop this size can outrun the pump — and a run
that dropped records from the *buffer* would look exactly like a run the
store trimmed, while proving something else entirely. The script reports
both numbers so the two cannot be confused.
"""

import logging

import lumberjack

RETAIN = 10_000
WRITTEN = RETAIN * 3

handler = lumberjack.init(
    output_mode="rich", flush_interval=0.01, dump_last_n=0, retain=RETAIN
)

log = logging.getLogger("ingest")
for i in range(WRITTEN):
    log.debug("row %d processed", i)
    if (i + 1) % 2_000 == 0:
        lumberjack.flush()

lumberjack.flush()

store = lumberjack.current_store()
if store is None:
    raise SystemExit("init() did not take: no store")

held = store.recent(n=None)
print(f"WRITTEN={WRITTEN}")
print(f"HELD={len(held)}")
print(f"DROPPED={handler.dropped}")
print(f"FIRST={held[0].message}")
print(f"LAST={held[-1].message}")
