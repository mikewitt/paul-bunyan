"""An inner loop inside an outer one, and the total nobody stated.

The ratio between an enclosing loop's period and an enclosed one's *is* the
inner loop's total — containment and the total come from the same
measurement. Nothing here says "20"; the display works it out from how often
each line recurs, and only claims it once the ratio has held across
consecutive polls.

A deliberately fat ratio (20:1). The assertion is that a determinate bar
appears near it, never that timing produced an exact number — Principle 10
licenses the display to be imprecise, so a test demanding precision would be
demanding something the design refuses to promise.
"""

import logging
import time

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.05, dump_last_n=0)

log = logging.getLogger("reconcile")
for batch in range(14):
    log.debug("reconciling batch %d", batch)
    for row in range(20):
        log.debug("compared row %d against ledger", row)
        time.sleep(0.015)

lumberjack.flush()
