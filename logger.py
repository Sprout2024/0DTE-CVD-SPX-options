from __future__ import annotations

import logging
import sys

from config import Config


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