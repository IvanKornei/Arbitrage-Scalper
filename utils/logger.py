"""
utils/logger.py – Structured logging with millisecond timestamps.

Features:
  • Sub-millisecond timestamps in every log line.
  • Colour-coded levels in the console (via ANSI codes).
  • Simultaneous output to stdout and a rotating file.
  • One shared root logger; per-module loggers inherit level automatically.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional


# ── ANSI colour map ───────────────────────────────────────────────────────────

_COLORS = {
    "DEBUG":    "\033[36m",    # cyan
    "INFO":     "\033[32m",    # green
    "WARNING":  "\033[33m",    # yellow
    "ERROR":    "\033[31m",    # red
    "CRITICAL": "\033[35m",    # magenta
}
_RESET = "\033[0m"


class _ColourFormatter(logging.Formatter):
    """
    Adds colour to the level name and includes milliseconds in the timestamp.
    Format:
        2024-06-15 14:23:45.123 | INFO     | module_name:42 | message
    """

    _FMT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
    _DATE_FMT = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        colour = _COLORS.get(record.levelname, "")
        record.levelname = f"{colour}{record.levelname}{_RESET}"
        formatter = logging.Formatter(self._FMT, datefmt=self._DATE_FMT)
        return formatter.format(record)


class _PlainFormatter(logging.Formatter):
    _FMT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
    _DATE_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        super().__init__(self._FMT, datefmt=self._DATE_FMT)


_initialised = False


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Call once at application startup.
    Idempotent – subsequent calls are no-ops.
    """
    global _initialised
    if _initialised:
        return
    _initialised = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColourFormatter())
    root.addHandler(console)

    # ── Rotating file handler ─────────────────────────────────────────────────
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=50 * 1024 * 1024,   # 50 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(_PlainFormatter())
        root.addHandler(file_handler)

    # Silence noisy third-party loggers
    for noisy in ("websockets", "aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info(
        "Logging initialised | level=%s | file=%s",
        level,
        log_file or "stdout only",
    )


def get_logger(name: str) -> logging.Logger:
    """
    Returns a module-level logger.  Always call this instead of
    logging.getLogger() directly so the name is consistently scoped.
    """
    return logging.getLogger(name)
