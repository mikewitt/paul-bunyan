"""Two threads, one loop each, and no bookkeeping to make that work.

The claim that separates this from `tqdm`: concurrency falls out of the
capture mechanism rather than being a feature. Neither thread declares a
position or a depth; each just logs, and attribution happens at write time
from what `LogRecord` already carries.
"""

import logging
import threading
import time

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.05, dump_last_n=0)

log = logging.getLogger("pipeline")


def extract() -> None:
    for row in range(120):
        log.debug("fetched row %d from source table", row)
        time.sleep(0.012)


def load() -> None:
    for batch in range(120):
        log.debug("wrote batch %d to warehouse", batch)
        time.sleep(0.012)


workers = [threading.Thread(target=extract), threading.Thread(target=load)]
for worker in workers:
    worker.start()
for worker in workers:
    worker.join()

lumberjack.flush()
