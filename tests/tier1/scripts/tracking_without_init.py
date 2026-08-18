"""The tracking API in a library that its application never initialised.

Design Principle 4: `track()` and `task()` must work without `init()`, and
absent a session they are inert — no store to write to, so nothing written,
and no log line either. A library may call them; what they *produce* is the
application's decision, never the library's.

Nothing is configured here on purpose. With no OTel and no `init()`, the
correct output is none at all.
"""

import logging

import lumberjack

logging.getLogger("app").info("a library logging as it works")

for _ in lumberjack.track(range(50), name="ingest", total=50):
    pass

with lumberjack.task("outer") as outer:
    outer.subtask("inner").end()

print("COMPLETED")
