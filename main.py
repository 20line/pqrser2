"""Точка входа: запускает Telegram Bot + Watcher Engine (п.12 ТЗ — Windows
Task Scheduler запускает именно этот скрипт)."""

from __future__ import annotations

import asyncio
import logging

from avito_watcher.app import run


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger("avito_watcher").info("Остановлено пользователем (Ctrl+C)")


if __name__ == "__main__":
    main()
