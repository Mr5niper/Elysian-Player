"""Logging setup.

Writes to a rotating file next to the settings, so a user can send a log
without needing a console build. Failures stay non-fatal -- the point is that
they leave evidence instead of vanishing.
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_FILE = Path.home() / ".elysian_player.log"
MAX_BYTES = 512 * 1024
BACKUPS = 2

_configured = False


def setup() -> logging.Logger:
    global _configured
    root = logging.getLogger("elysian")
    if _configured:
        return root

    root.setLevel(logging.DEBUG if os.environ.get("ELYSIAN_DEBUG")
                  else logging.INFO)
    root.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(threadName)-18s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS,
            encoding="utf-8", delay=True)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass

    # Under --windowed there is no console, so this only does anything when
    # running from source.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    _configured = True
    return root


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"elysian.{name}")
