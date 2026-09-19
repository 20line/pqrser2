"""Логирование в файл с ротацией (п.13 ТЗ)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, max_bytes: int = 5_000_000, backup_count: int = 5) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("avito_watcher")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger  # уже настроен (например, в тестах)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        log_dir / "avito_watcher.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


def dump_html_for_debug(log_dir: Path, profile_id: int, html: str) -> Path:
    """Сохраняет HTML страницы для разбора при подозрении на смену вёрстки."""
    debug_dir = log_dir / "html_dumps"
    debug_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = debug_dir / f"profile_{profile_id}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    return path
