"""Standalone script: init lumberjack in rich (live bar) mode, then log the
same line N times from a loop — what a developer's debug logging inside a
loop actually looks like.

The parent test (test_exit_paths.py) checks the whole premise in a real
process: the loop's lines don't scroll, a bar is drawn instead, every record
still lands in the store, and a WARNING still gets through. The env knobs
exist so one script can cover the clean-exit case and both sides of the
lossy-renderer diagnostic dump — buffer already drained by the pump, and
buffer still full at exit.
"""

import logging
import os

import lumberjack
from lumberjack.store import SQLiteRecordStore

db_path = os.environ["LUMBERJACK_TEST_DB_PATH"]
record_count = int(os.environ.get("LUMBERJACK_TEST_RECORD_COUNT", "200"))
dump_last_n = int(os.environ.get("LUMBERJACK_TEST_DUMP_LAST_N", "0"))
flush_interval = float(os.environ.get("LUMBERJACK_TEST_FLUSH_INTERVAL", "0.01"))
final_flush = os.environ.get("LUMBERJACK_TEST_FINAL_FLUSH", "1") == "1"

store = SQLiteRecordStore(db_path)
lumberjack.init(
    output_mode="rich",
    store=store,
    flush_interval=flush_interval,
    dump_last_n=dump_last_n,
)

logger = logging.getLogger("script")
for i in range(record_count):
    logger.info("processing item %d", i)
logger.warning("something looked odd")

if final_flush:
    # An app that finishes cleanly: everything is in the store before exit,
    # so the bar's last frame shows the final count rather than whatever the
    # timer happened to have drawn.
    lumberjack.flush()
