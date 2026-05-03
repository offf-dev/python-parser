"""Конфигурация logging — отдельно от app.py чтобы любой модуль мог получить logger."""

import logging
import os
import warnings
from logging.handlers import TimedRotatingFileHandler

from bs4 import MarkupResemblesLocatorWarning


_configured = False


def setup_logging() -> logging.Logger:
    """Идемпотентная настройка: дёрнуть из любого модуля, эффект только при первом вызове."""
    global _configured
    logger = logging.getLogger("parser")

    if _configured:
        return logger

    os.makedirs("logs", exist_ok=True)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = TimedRotatingFileHandler(
        filename="logs/parser.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # APScheduler-логи в тот же файл
    aps_logger = logging.getLogger("apscheduler")
    aps_logger.setLevel(logging.INFO)
    aps_logger.addHandler(handler)
    aps_logger.propagate = False

    logging.captureWarnings(True)
    warnings.filterwarnings("always", category=MarkupResemblesLocatorWarning)
    warnings.filterwarnings(
        "ignore",
        message=".*strip_cdata.*",
        category=UserWarning,
        module="bs4.builder._lxml",
    )

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return setup_logging()