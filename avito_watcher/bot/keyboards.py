"""Inline-клавиатуры (п.9 ТЗ)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from avito_watcher.models import GlobalSettings, Profile


def main_menu(paused_or_captcha: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📦 Мои товары", callback_data="main:products")
    b.button(text="➕ Добавить товар", callback_data="main:add")
    resume_label = "▶️ Возобновить всё" if paused_or_captcha else "⏸ Пауза всех"
    b.button(text=resume_label, callback_data="main:pause_toggle")
    b.button(text="⚙️ Настройки", callback_data="main:settings")
    b.button(text="📊 Статистика", callback_data="main:stats")
    b.adjust(1)
    return b.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В меню", callback_data="main")
    return b.as_markup()


def products_list(profiles: list[Profile]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in profiles:
        status = "🟢" if not p.paused else "⏸"
        b.button(text=f"{status} {p.name}", callback_data=f"profile:{p.id}")
    b.button(text="➕ Добавить товар", callback_data="main:add")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(1)
    return b.as_markup()


def profile_card(profile: Profile) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить", callback_data=f"profile:{profile.id}:edit")
    pause_label = "▶️ Возобновить" if profile.paused else "⏸ Пауза"
    b.button(text=pause_label, callback_data=f"profile:{profile.id}:pause_toggle")
    b.button(text="🔍 Проверить сейчас", callback_data=f"profile:{profile.id}:check_now")
    b.button(text="📈 Последние найденные", callback_data=f"profile:{profile.id}:history")
    b.button(text="🗑 Удалить", callback_data=f"profile:{profile.id}:delete")
    b.button(text="⬅️ К списку товаров", callback_data="main:products")
    b.adjust(1)
    return b.as_markup()


def edit_profile_menu(profile_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔗 Ссылка на поиск", callback_data=f"profile:{profile_id}:edit:url")
    b.button(text="✏️ Название", callback_data=f"profile:{profile_id}:edit:name")
    b.button(text="💰 Макс. цена", callback_data=f"profile:{profile_id}:edit:price")
    b.button(text="🚫 Стоп-слова", callback_data=f"profile:{profile_id}:edit:stopwords")
    b.button(text="⬅️ Назад", callback_data=f"profile:{profile_id}")
    b.adjust(1)
    return b.as_markup()


def cancel_only(callback_data: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Отмена", callback_data=callback_data)
    return b.as_markup()


def confirm_delete(profile_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Да, удалить", callback_data=f"profile:{profile_id}:delete:confirm")
    b.button(text="Отмена", callback_data=f"profile:{profile_id}")
    b.adjust(2)
    return b.as_markup()


def add_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Сохранить", callback_data="add:confirm")
    b.button(text="Отмена", callback_data="add:cancel")
    b.adjust(2)
    return b.as_markup()


def settings_menu(settings: GlobalSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    digest_label = f"Digest: {'вкл' if settings.digest_mode else 'выкл'} ({settings.digest_interval_minutes} мин)"
    b.button(text=digest_label, callback_data="settings:digest_toggle")
    if settings.digest_mode:
        b.button(text="⏱ Интервал digest", callback_data="settings:digest_interval")
    quiet_label = f"Тихие часы: {'вкл' if settings.quiet_hours_enabled else 'выкл'} ({settings.quiet_hours_start}–{settings.quiet_hours_end})"
    b.button(text=quiet_label, callback_data="settings:quiet_toggle")
    if settings.quiet_hours_enabled:
        b.button(text="🕐 Диапазон тихих часов", callback_data="settings:quiet_range")
    b.button(text="⚙️ Дополнительно", callback_data="settings:advanced")
    b.button(text="⬅️ В меню", callback_data="main")
    b.adjust(1)
    return b.as_markup()


def settings_advanced_menu(settings: GlobalSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=f"Бюджет запросов: {settings.request_budget_seconds} сек",
        callback_data="settings:budget",
    )
    b.button(text="⬅️ Назад", callback_data="main:settings")
    b.adjust(1)
    return b.as_markup()
