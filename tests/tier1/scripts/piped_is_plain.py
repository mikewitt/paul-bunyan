"""No `output_mode` given, and stderr is not a terminal.

Design Principle 5: never assume a human is watching. Detection picks the
consumer, and a pipe gets write-through text — every record, in order, with
no cursor control — because in-place redraw is corruption to anything that
is not a terminal.

The `loop_to_bar.py` pair forces rich onto a pipe to prove a warning still
survives the collapse. This asserts the other switch: left to itself, the
display does not collapse anything at all.
"""

import logging

import lumberjack

lumberjack.init(flush_interval=0.01, dump_last_n=0)

log = logging.getLogger("ingest")
for i in range(20):
    log.info("processing item %d", i)

lumberjack.flush()
