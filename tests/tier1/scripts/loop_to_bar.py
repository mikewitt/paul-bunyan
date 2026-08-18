"""A loop with a debug line in it — the shape lumberjack exists for.

Rung 1 of the value ladder: `init()` and nothing else. The loop logs the
same line 200 times, which without lumberjack is 200 lines of scroll, and
with it is one bar.

`dump_last_n=0` turns off the `atexit` diagnostic dump. Leave it on and the
tail of the store prints after the display comes down, which is a useful
diagnostic and ruinous to a test asserting on what reached the screen.
"""

import logging

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.01, dump_last_n=0)

logger = logging.getLogger("ingest")
for i in range(200):
    logger.info("processing item %d", i)
logger.warning("something looked odd")

# An app that finishes cleanly. Everything is in the store before exit, so
# the bar's last frame shows the final count rather than whatever the timer
# happened to have drawn.
lumberjack.flush()

store = lumberjack.current_store()
if store is None:
    raise SystemExit("init() did not take: no store")
# stdout, deliberately: the live display owns stderr and never touches this,
# so the parent can read a fact off one stream and the frame off the other.
print(f"STORED={len(store.recent(n=None))}")
