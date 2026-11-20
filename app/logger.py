from __future__ import annotations

import os
import sys

from loguru import logger


def setup_logger(level: str = "INFO") -> None:
    """Loguru konfiguratsiyasi (Serverless va VPS uchun mos)."""
    logger.remove()
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    logger.add(sys.stdout, level=level.upper(), format=log_format, colorize=False)

    # Serverless (Vercel) muhitida fayl tizimi faqat /tmp da yoziladi,
    # VPS/lokal rejimda esa logs/ papkasiga fayl yoziladi.
    if not os.getenv("VERCEL"):
        try:
            os.makedirs("logs", exist_ok=True)
            logger.add(
                "logs/userbot.log",
                level=level.upper(),
                rotation="10 MB",
                retention="7 days",
                compression="zip",
                encoding="utf-8",
            )
        except OSError:
            pass
