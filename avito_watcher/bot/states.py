"""FSM-состояния диалогов бота (п.9 ТЗ)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddProfileStates(StatesGroup):
    waiting_url = State()
    waiting_name = State()
    waiting_price_ceiling = State()
    waiting_stop_words = State()


class EditProfileStates(StatesGroup):
    waiting_url = State()
    waiting_name = State()
    waiting_price_ceiling = State()
    waiting_stop_words = State()


class SettingsStates(StatesGroup):
    waiting_digest_interval = State()
    waiting_budget_seconds = State()
    waiting_quiet_range = State()
