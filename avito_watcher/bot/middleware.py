"""Доступ только владельцу: остальные обращения молча игнорируются (п.9 ТЗ)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from avito_watcher.settings_cache import SettingsCache

logger = logging.getLogger("avito_watcher")


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, settings_cache: SettingsCache):
        self.settings_cache = settings_cache
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        owner_chat_id = self.settings_cache.get().owner_chat_id
        if user is None or user.id != owner_chat_id:
            if user is not None:
                logger.info("Игнорирую обращение от постороннего пользователя id=%s", user.id)
            return None  # без ответа — не подтверждаем существование бота
        return await handler(event, data)
