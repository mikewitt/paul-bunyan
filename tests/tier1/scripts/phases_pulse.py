"""Stages announced from one line, each running its own loop elsewhere.

The false parent, from the outside. `log.info("stage %s: starting", …)` is a
slow repeating source — once per stage — and each stage's own loop is a fast
one, so period ordering reads the announcement as enclosing them and offers
a ratio as the inner loop's total. It is not enclosing them: lexically the
stage loops are top-level in their own functions, and the announcement line
merely runs before each call.

Nothing in the log stream can tell "A encloses B" from "A precedes B", so
the display must not claim the total that ordering fabricated. Static
structure supplies the veto rather than an answer — the AST can see the
child loop is top-level in another function, which is enough to withhold
the number without being able to say what the right one is.

Slow, and irreducibly: the ratio has to be measured from real inter-arrival
times across consecutive real polls before there is anything to withhold.
"""

import logging
import time

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.05, dump_last_n=0)

log = logging.getLogger("etl")


def discover() -> None:
    for i in range(40):
        log.debug("found input file %d", i)
        time.sleep(0.012)


def parse() -> None:
    for i in range(40):
        log.debug("parsed record %d", i)
        time.sleep(0.012)


def join() -> None:
    for i in range(40):
        log.debug("joined row %d against reference data", i)
        time.sleep(0.012)


for name, stage in (("discover", discover), ("parse", parse), ("join", join)):
    log.info("stage %s: starting", name)
    stage()

lumberjack.flush()
