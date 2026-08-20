from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import Config

_UTC = timezone.utc


class _UTCFormatter(logging.Formatter):
    """Log timestamps in UTC so logs line up with the IBKR API clock
    regardless of the machine's local timezone (e.g. CST / UTC+8)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, _UTC)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


def setup_logging(cfg: Config) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file))
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    for h in logging.getLogger().handlers:
        h.setFormatter(_UTCFormatter(fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                     datefmt="%Y-%m-%d %H:%M:%S"))