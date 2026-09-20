"""Точечные тесты на обработчики бота, где логика достаточно самостоятельна,
чтобы вызывать её напрямую без полного aiogram Dispatcher/polling."""

from pathlib import Path

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from avito_watcher.bot import handlers
from avito_watcher.bot.states import SettingsStates
from avito_watcher.db import Database
from avito_watcher.settings_cache import SettingsCache


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)


class FakeCallbackQuery:
    def __init__(self):
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.ensure_global_settings(owner_chat_id=42)
    yield database
    await database.close()


def make_state() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    return FSMContext(storage=storage, key=key)


async def test_settings_advanced_clears_fsm_state_as_cancel_target(db: Database):
    """Регрессия: "settings:advanced" — это ещё и цель кнопки «Отмена» для
    запроса бюджета запросов (SettingsStates.waiting_budget_seconds). Без
    сброса состояния следующее произвольное текстовое сообщение владельца
    молча перехватывалось бы обработчиком settings_receive_budget, как
    будто это ответ на уже отменённый вопрос."""
    settings_cache = SettingsCache(db)
    await settings_cache.load()
    state = make_state()
    await state.set_state(SettingsStates.waiting_budget_seconds)

    cb = FakeCallbackQuery()
    await handlers.cb_settings_advanced(cb, state, settings_cache)

    assert await state.get_state() is None
    assert len(cb.message.edits) == 1
