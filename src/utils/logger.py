import sys

from loguru import logger

from src.config import settings


def setup_logger() -> None:
    """Configure loguru logger for the application."""
    logger.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # Console output
    logger.add(
        sys.stderr,
        format=log_format,
        level=settings.log_level,
        colorize=True,
    )

    # File output - rotated daily, kept for 30 days
    logger.add(
        "logs/fifa_bet_{time:YYYY-MM-DD}.log",
        format=log_format,
        level=settings.file_log_level,
        rotation="00:00",
        retention="30 days",
        compression="gz",
    )

    # Separate file for alerts only
    logger.add(
        "logs/alerts_{time:YYYY-MM-DD}.log",
        format=log_format,
        level="INFO",
        rotation="00:00",
        retention="90 days",
        filter=lambda record: "alert" in record["extra"].get("category", ""),
    )
