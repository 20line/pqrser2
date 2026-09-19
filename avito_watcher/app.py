"""Сборка приложения: Telegram Bot и Watcher Engine работают в одном
процессе на общем event loop, обмениваясь состоянием через State Store
(SQLite) — отдельный процесс/IPC не нужен (п.4 ТЗ)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from playwright.async_api import async_playwright

from avito_watcher.bot.handlers import router
from avito_watcher.bot.middleware import OwnerOnlyMiddleware
from avito_watcher.config import AppConfig, load_config
from avito_watcher.db import Database
from avito_watcher.logging_setup import setup_logging
from avito_watcher.notifications import Notifier
from avito_watcher.scheduler import Scheduler
from avito_watcher.settings_cache import SettingsCache
from avito_watcher.watcher import WatcherEngine


async def run(config: Optional[AppConfig] = None) -> None:
    config = config or load_config()
    logger = setup_logging(config.log_dir, config.log_max_bytes, config.log_backup_count)
    logger.info("Запуск Avito Watcher Bot")

    db = Database(config.db_path)
    await db.connect()
    await db.ensure_global_settings(config.owner_chat_id)

    settings_cache = SettingsCache(db)
    await settings_cache.load()

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    owner_only = OwnerOnlyMiddleware(settings_cache)
    dp.message.middleware(owner_only)
    dp.callback_query.middleware(owner_only)
    dp.include_router(router)

    async def send_message(chat_id: int, text: str, **kwargs) -> None:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)

    notifier = Notifier(send_message, db, settings_cache.get, logger)
    scheduler = Scheduler(db, settings_cache.get)

    async with async_playwright() as playwright:
        watcher = WatcherEngine(
            db=db,
            scheduler=scheduler,
            notifier=notifier,
            settings_provider=settings_cache.get,
            browser_profile_dir=config.browser_profile_dir,
            log_dir=config.log_dir,
            logger=logger,
        )
        await watcher.start(playwright)
        logger.info("Watcher Engine запущен, начинаю цикл проверок")

        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                db=db,
                scheduler=scheduler,
                watcher=watcher,
                notifier=notifier,
                settings_cache=settings_cache,
            ),
            name="telegram_polling",
        )
        watcher_task = asyncio.create_task(watcher.run_forever(), name="watcher_engine")
        digest_task = asyncio.create_task(notifier.digest_flush_loop(), name="digest_flush")

        tasks = (polling_task, watcher_task, digest_task)
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.error("Задача %s завершилась с ошибкой", task.get_name(), exc_info=exc)
        finally:
            logger.info("Останавливаю Avito Watcher Bot")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await watcher.stop()
            await bot.session.close()
            await db.close()
