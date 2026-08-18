"""Fixture module source strings shared by the display/model test suites.

Each constant below is a tiny, real Python module written to `tmp_path` by
the tests that use it, so `static.py` has actual source to parse rather than
a mock. Line numbers inside these strings are asserted against by name
(`file:lineno`), so they move byte-for-byte between test files rather than
being retyped.

- `SIBLINGS` — two log lines in one loop body, at different paces: what the
  AST says is one row even though the runtime signal alone would not.
- `NESTED` — a loop logging once per batch, with a loop inside it logging
  once per row: real lexical containment.
- `PHASES` — an announcement line calling a helper with its own loop, in
  another function: period ordering reads this as containment, and the AST
  does not, because lexically it is not.
- `CALLER` — `PHASES`' announcement line, alone, in its own file: the
  cross-file version of the same refusal.
- `NOT_IN_A_LOOP` — one call site with no enclosing loop at all.
- `MIXED_BODY` — a loop body with a nested loop that itself contains a
  conditional call site, so a merged row's members disagree on how the
  runtime model froze their containment.
- `SEQUENCE` — one loop narrating three stages per iteration, slow enough
  that the stage within the iteration is the only legible signal (#53).
- `BRANCHING` — the same shape as `SEQUENCE`, but the middle stage only
  fires on some iterations, so the body has no stable order.
- `FSTRING` — the same shape again, with the last stage written as an
  f-string, which destroys its template at the call site.
- `LONE` — a single call site inside a loop: `1 of 1` forever.
- `ONE_NAMEABLE` — two call sites, one of them an f-string, so only one can
  ever be the stage on show.
"""

from __future__ import annotations

from lumberjack.renderers.progress import CONTAINMENT_CONFIRMATIONS, DEFAULT_MIN_REPEATS

SIBLINGS = """\
import logging

log = logging.getLogger(__name__)


def run():
    for row in range(400):
        log.debug("row %d: parsed", row)
        log.debug("row %d: validated", row)
"""

NESTED = """\
import logging

log = logging.getLogger(__name__)


def reconcile():
    for batch in range(24):
        log.debug("reconciling batch %d", batch)
        for row in range(20):
            log.debug("compared row %d against ledger", row)
"""

PHASES = """\
import logging

log = logging.getLogger(__name__)


def write():
    for i in range(40):
        log.debug("wrote partition %d", i)


def run():
    for number in range(4):
        log.info("stage %d", number)
        write()
"""

CALLER = """\
import logging

log = logging.getLogger(__name__)


def run():
    for number in range(4):
        log.info("stage %d", number)
"""

NOT_IN_A_LOOP = """\
import logging

log = logging.getLogger(__name__)


def emit(i):
    log.debug("emitted %d", i)
"""

MIXED_BODY = """\
import logging

log = logging.getLogger(__name__)


def process():
    for batch in range(8):
        log.debug("batch %d", batch)
        for row in range(9):
            log.debug("row %d", row)
            if row % 3 == 0:
                log.debug("checkpoint %d", row)
"""

# `sequence` in miniature: three stages narrating one slow body. The loop is
# on line 7 and the call sites on 8, 9 and 10.
SEQUENCE = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        log.debug("batch %d: fetching manifest", batch)
        log.debug("batch %d: validating checksums", batch)
"""

STAGES = (
    "batch %d: opening connection",
    "batch %d: fetching manifest",
    "batch %d: validating checksums",
)

# `transform` in miniature: the middle line only fires sometimes, so the
# sequence a reader would see depends on the data rather than on the source.
BRANCHING = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        if batch % 2:
            log.warning("batch %d: retrying", batch)
        log.debug("batch %d: committing", batch)
"""

# A body whose last line is an f-string. The template is destroyed at the call
# site, so `record.msg` arrives as rendered text and that source is unplaceable.
FSTRING = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        log.debug("batch %d: fetching manifest", batch)
        log.debug(f"batch {batch}: far wider than any real template in this body")
"""

# Two call sites of which only one can ever be named, which is `LONE` wearing
# a second line. The AST counts two and the display can reach one.
ONE_NAMEABLE = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        log.debug(f"batch {batch}: committing")
"""

# `phases`' announcement line: one call site, so position is 1 of 1 forever.
LONE = """\
import logging

log = logging.getLogger(__name__)


def run():
    for batch in range(6):
        log.info("stage %d", batch)
"""

#: Enough polls for a source to reach `min_repeats` and then for a candidate
#: pairing to be confirmed. Confirmations only count polls that brought new
#: records, so this is a number of *cycles*, not a number of redraws.
SETTLED_CYCLES = DEFAULT_MIN_REPEATS + CONTAINMENT_CONFIRMATIONS
