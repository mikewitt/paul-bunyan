"""A loop slow enough that the row above it cannot answer "is it moving?".

The legibility criterion, from the outside. Five stages per iteration at
roughly 1.3s an iteration: the loop row ticks once every 1.3s, which is too
rare to distinguish running from hung, so a second row is drawn beneath it
counting position *within* the current iteration. `siblings_merge.py` has
the identical shape two hundred times faster and earns no such row.

Slow on purpose and slow irreducibly: `MIN_LEGIBLE_PERIOD` is 1.0s and is
not settable from the public API, rightly — a display rule that a test can
turn off is not a rule. So this burns real seconds, and is marked `slow`.
"""

import logging
import time

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.05, dump_last_n=0)

log = logging.getLogger("ingest")
for batch in range(4):
    log.debug("batch %d: opening connection", batch)
    time.sleep(0.26)
    log.debug("batch %d: fetching manifest", batch)
    time.sleep(0.26)
    log.debug("batch %d: validating checksums", batch)
    time.sleep(0.26)
    log.debug("batch %d: writing output", batch)
    time.sleep(0.26)
    log.debug("batch %d: committing", batch)
    time.sleep(0.26)

lumberjack.flush()
