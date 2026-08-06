"""
Structured logging setup for JARVIS.

Configures the root logger once, at startup, so every module in the app
can just write `logger = logging.getLogger(__name__)` and get consistent
formatting without configuring anything itself -- every other logger is a
child of the root logger and inherits this setup automatically.
"""

import logging
import sys

from app.config import settings


def setup_logging() -> None:
    """Call once, before the app starts handling requests (see main.py)."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
