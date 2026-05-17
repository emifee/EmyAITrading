"""
logger.py — Structured logging setup using Loguru.

Provides console + file logging with daily rotation.
All modules import the logger from here.
"""

import os
import sys
from loguru import logger

import config


def setup_logger():
    """Configure loguru with console and file sinks."""

    # Remove default handler
    logger.remove()

    # Console output — colored, concise
    logger.add(
        sys.stdout,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        level="INFO",
        colorize=True,
    )

    # File output — detailed, rotated daily
    os.makedirs(config.LOG_DIR, exist_ok=True)

    logger.add(
        os.path.join(config.LOG_DIR, "trading_{time:YYYY-MM-DD}.log"),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        level="DEBUG",
        rotation=config.LOG_ROTATION,
        retention=config.LOG_RETENTION,
        compression="zip",
        enqueue=True,  # Thread-safe
    )

    # Error-only log for critical issues
    logger.add(
        os.path.join(config.LOG_DIR, "errors_{time:YYYY-MM-DD}.log"),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}\n{exception}"
        ),
        level="ERROR",
        rotation=config.LOG_ROTATION,
        retention=config.LOG_RETENTION,
        compression="zip",
        enqueue=True,
    )

    logger.info("Logger initialized")
    return logger


# Initialize on import
log = setup_logger()
