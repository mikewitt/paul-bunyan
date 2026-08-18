"""Four log lines in one loop body — one loop, so one row.

The shape that makes the display-unit argument concrete. These four calls
are one loop by any reasonable reading, and a reader wants one row for it
counting *rows processed*, not four rows counting log calls. Source location
is the right identity for a call site and the wrong unit for a display.

Fast on purpose: at this rate an intra-iteration bar would be a blur, so the
legibility criterion should refuse the second row here and grant it to a
slow loop with the same shape.
"""

import logging

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.01, dump_last_n=0)

log = logging.getLogger("ingest")
for row in range(100):
    log.debug("row %d: parsed", row)
    log.debug("row %d: schema validated", row)
    log.debug("row %d: enriched from cache", row)
    log.debug("row %d: emitted downstream", row)

lumberjack.flush()

store = lumberjack.current_store()
if store is None:
    raise SystemExit("init() did not take: no store")
print(f"STORED={len(store.recent(n=None))}")
