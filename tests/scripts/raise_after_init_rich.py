"""Standalone script: init lumberjack in rich mode, log a few records, raise.

The parent test (test_integration.py) skips this when `rich` isn't
installed.
"""

import logging

import lumberjack

lumberjack.init(output_mode="rich")
logger = logging.getLogger("script")
logger.info("starting")
raise RuntimeError("boom")
