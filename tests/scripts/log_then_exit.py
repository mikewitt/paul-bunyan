"""Standalone script: init lumberjack with a file-backed sqlite store, log N
records, then exit normally (no explicit flush/shutdown call).

The parent test (test_integration.py) re-opens the sqlite file after this
process exits and asserts all N records are present, proving the atexit-time
flush ran.
"""

import logging
import os

import lumberjack
from lumberjack.store import SQLiteRecordStore

db_path = os.environ["LUMBERJACK_TEST_DB_PATH"]
record_count = int(os.environ.get("LUMBERJACK_TEST_RECORD_COUNT", "10"))

store = SQLiteRecordStore(db_path)
lumberjack.init(output_mode="plain", store=store)

logger = logging.getLogger("script")
for i in range(record_count):
    logger.info("record %d", i)
