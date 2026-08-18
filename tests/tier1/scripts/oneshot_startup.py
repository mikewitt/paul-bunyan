"""Startup narration: every line fires exactly once.

Nothing here repeats, so nothing here has a period, so no loop row can
honestly be drawn for it. The display should say so by not claiming one —
the heartbeat may still show that records are arriving, but a source that
fired once is not a loop and must not be counted as iterating.

The negative case for the whole inference layer, and the honest half of the
matplotlib finding: libraries narrate boundaries, not work.
"""

import logging

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.01, dump_last_n=0)

log = logging.getLogger("startup")
log.info("reading configuration from /etc/pipeline.toml")
log.info("connecting to warehouse at db.internal:5432")
log.info("negotiated protocol version 3")
log.info("warming schema cache")
log.info("registered 14 table mappings")
log.info("ready")

lumberjack.flush()
