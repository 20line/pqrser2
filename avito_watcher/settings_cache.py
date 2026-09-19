"""Кэш GlobalSettings в памяти с синхронным доступом (write-through к SQLite).

Scheduler/Watcher/Notifier читают настройки синхронно в горячем пути цикла
проверки, поэтому актуальная копия держится в памяти процесса и
обновляется при любом изменении через Telegram-меню «⚙️ Настройки».
"""

from __future__ import annotations

from typing import Optional

from avito_watcher.db import Database
from avito_watcher.models import GlobalSettings


class SettingsCache:
    def __init__(self, db: Database):
        self.db = db
        self._settings: Optional[GlobalSettings] = None

    async def load(self) -> GlobalSettings:
        self._settings = await self.db.get_settings()
        return self._settings

    def get(self) -> GlobalSettings:
        assert self._settings is not None, "SettingsCache не инициализирован, вызовите load()"
        return self._settings

    async def update(self, **kwargs) -> GlobalSettings:
        await self.db.update_settings(**kwargs)
        self._settings = await self.db.get_settings()
        return self._settings
