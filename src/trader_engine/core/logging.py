from __future__ import annotations

import logging

from trader_engine.core.config import LoggingConfig


def configure_logging(config: LoggingConfig) -> None:
    logging.basicConfig(
        level=getattr(logging, config.level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
