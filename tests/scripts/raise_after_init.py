"""Standalone script: init lumberjack in plain mode, log a few records, raise.

Run via subprocess (see test_exit_paths.py) so sys.excepthook is exercised
for real — pytest owns exception handling for in-process test functions, so
this can't be tested any other way.
"""

import logging

import lumberjack

lumberjack.init(output_mode="plain")
logger = logging.getLogger("script")
logger.info("starting")
logger.info("about to fail")
raise RuntimeError("boom")
