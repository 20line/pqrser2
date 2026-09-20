"""Обработчики команд/кнопок Telegram-бота (п.9 ТЗ). Доступ ограничен
owner_chat_id через OwnerOnlyMiddleware, регистрируемую в app.py."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from avito_watcher.bot import formatting, keyboards
from avito_watcher.bot.states import AddProfileStates, EditProfileStates, SettingsStates
from avito_watcher.db import Database
from avito_watcher.scheduler import Scheduler
from avito_watcher.settings_cache import SettingsCache
from avito_watcher.textutils import escape_html
from avito_watcher.watcher import WatcherEngine

router = Router(name="avito_watcher")

SKIP_WORDS = {"пропустить", "skip", "-", "нет", "убрать"}


def parse_optional_price(text: str) -> Optional[int]:
    t = text.strip().lower()
    if t in SKIP_WORDS:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        raise ValueError("Не удалось распознать число")
    return int(digits)


def parse_stop_words(text: str) -> list[str]:
    t = text.strip().lower()
    if t in SKIP_WORDS:
        return []
    return [w.strip() for w in text.split(",") if w.strip()]


def validate_search_url(text: str) -> Optional[str]:
    t = text.strip()
    if not t.lower().startswith("http") or "avito.ru" not in t.lower():
        return None
    return t


def validate_quiet_range(text: str) -> Optional[tuple[str, str]]:
    t = text.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})$", t)
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 < 24 and 0 <= m1 < 60 and 0 <= h2 < 24 and 0 <= m2 < 60):
        return None
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"


async def is_globally_paused(db: Database, settings_cache: SettingsCache) -> bool:
    if settings_cache.get().paused_all:
        return True
    captcha_state = await db.get_captcha_state()
    return captcha_state.active


async def render_main_menu(message: Message, db: Database, settings_cache: SettingsCache) -> None:
    paused = await is_globally_paused(db, settings_cache)
    await message.answer(
        "Главное меню Avito Watcher Bot:",
        reply_markup=keyboards.main_menu(paused),
    )


async def edit_main_menu(cb: CallbackQuery, db: Database, settings_cache: SettingsCache) -> None:
    paused = await is_globally_paused(db, settings_cache)
    await cb.message.edit_text(
        "Главное меню Avito Watcher Bot:",
        reply_markup=keyboards.main_menu(paused),
    )


# ---------------------------------------------------------------------------
# /start и главное меню
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database, settings_cache: SettingsCache) -> None:
    await state.clear()
    await render_main_menu(message, db, settings_cache)


@router.callback_query(F.data == "main")
async def cb_main(cb: CallbackQuery, state: FSMContext, db: Database, settings_cache: SettingsCache) -> None:
    await state.clear()
    await edit_main_menu(cb, db, settings_cache)
    await cb.answer()


@router.callback_query(F.data == "main:pause_toggle")
async def cb_pause_toggle(
    cb: CallbackQuery, db: Database, settings_cache: SettingsCache, watcher: WatcherEngine
) -> None:
    paused = await is_globally_paused(db, settings_cache)
    if paused:
        await watcher.resume_all(manual=True)
        await cb.answer("Проверки возобновлены")
    else:
        await watcher.pause_all_manual()
        await cb.answer("Все проверки на паузе")
    await edit_main_menu(cb, db, settings_cache)


# ---------------------------------------------------------------------------
# 📦 Мои товары
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "main:products")
async def cb_products(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    profiles = await db.list_profiles()
    if not profiles:
        await cb.message.edit_text(
            "Пока нет ни одного отслеживаемого товара.", reply_markup=keyboards.products_list([])
        )
    else:
        await cb.message.edit_text("📦 Ваши товары:", reply_markup=keyboards.products_list(profiles))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^profile:(\d+)$"))
async def cb_profile_card(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден (возможно, уже удалён)", show_alert=True)
        profiles = await db.list_profiles()
        await cb.message.edit_text("📦 Ваши товары:", reply_markup=keyboards.products_list(profiles))
        return
    found_today = await db.count_notifications_today(profile_id)
    text = formatting.profile_card_text(profile, found_today)
    await cb.message.edit_text(text, reply_markup=keyboards.profile_card(profile))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^profile:(\d+):pause_toggle$"))
async def cb_profile_pause_toggle(cb: CallbackQuery, db: Database) -> None:
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден", show_alert=True)
        return
    await db.set_profile_paused(profile_id, not profile.paused)
    await db.log_event("owner_action", profile_id, f"pause_toggle:{not profile.paused}")
    profile = await db.get_profile(profile_id)
    found_today = await db.count_notifications_today(profile_id)
    await cb.message.edit_text(
        formatting.profile_card_text(profile, found_today), reply_markup=keyboards.profile_card(profile)
    )
    await cb.answer("Готово")


@router.callback_query(F.data.regexp(r"^profile:(\d+):check_now$"))
async def cb_profile_check_now(cb: CallbackQuery, db: Database, scheduler: Scheduler) -> None:
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден", show_alert=True)
        return
    scheduler.request_check_now(profile_id)
    await db.log_event("owner_action", profile_id, "check_now_requested")
    await cb.answer("Добавлено в очередь на внеплановую проверку")


@router.callback_query(F.data.regexp(r"^profile:(\d+):history$"))
async def cb_profile_history(cb: CallbackQuery, db: Database) -> None:
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден", show_alert=True)
        return
    rows = await db.get_recent_notifications(profile_id, limit=10)
    text = formatting.history_text(profile.name, rows)
    await cb.message.edit_text(text, reply_markup=keyboards.profile_card(profile))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^profile:(\d+):delete$"))
async def cb_profile_delete_confirm_ask(cb: CallbackQuery, db: Database) -> None:
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден", show_alert=True)
        return
    await cb.message.edit_text(
        f'Удалить товар «{escape_html(profile.name)}»? Это действие нельзя отменить.',
        reply_markup=keyboards.confirm_delete(profile_id),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^profile:(\d+):delete:confirm$"))
async def cb_profile_delete_do(cb: CallbackQuery, db: Database) -> None:
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    name = profile.name if profile else str(profile_id)
    await db.delete_profile(profile_id)
    await db.log_event("owner_action", None, f"profile_deleted:{name}")
    profiles = await db.list_profiles()
    await cb.message.edit_text(
        f'Товар «{escape_html(name)}» удалён.\n\n📦 Ваши товары:', reply_markup=keyboards.products_list(profiles)
    )
    await cb.answer("Удалено")


# ---------------------------------------------------------------------------
# ✏️ Изменить товар
# ---------------------------------------------------------------------------

_EDIT_FIELD_PROMPTS = {
    "url": "Пришли новую ссылку на поиск Avito.",
    "name": "Как теперь назвать этот товар?",
    "price": "Максимальная цена для пометки «хорошая цена»? (число или «пропустить», чтобы убрать)",
    "stopwords": "Стоп-слова через запятую? (или «пропустить», чтобы убрать все)",
}

_EDIT_FIELD_STATE = {
    "url": EditProfileStates.waiting_url,
    "name": EditProfileStates.waiting_name,
    "price": EditProfileStates.waiting_price_ceiling,
    "stopwords": EditProfileStates.waiting_stop_words,
}


@router.callback_query(F.data.regexp(r"^profile:(\d+):edit$"))
async def cb_profile_edit_menu(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    profile_id = int(cb.data.split(":")[1])
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден", show_alert=True)
        return
    await cb.message.edit_text(
        f'Что изменить в товаре «{escape_html(profile.name)}»?', reply_markup=keyboards.edit_profile_menu(profile_id)
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^profile:(\d+):edit:(url|name|price|stopwords)$"))
async def cb_profile_edit_field(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    _, profile_id_str, _, field = cb.data.split(":")
    profile_id = int(profile_id_str)
    profile = await db.get_profile(profile_id)
    if profile is None:
        await cb.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(_EDIT_FIELD_STATE[field])
    await state.update_data(profile_id=profile_id)
    await cb.message.edit_text(
        _EDIT_FIELD_PROMPTS[field], reply_markup=keyboards.cancel_only(f"profile:{profile_id}:edit")
    )
    await cb.answer()


@router.message(EditProfileStates.waiting_url)
async def edit_receive_url(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    profile_id = data["profile_id"]
    url = validate_search_url(message.text or "")
    if url is None:
        await message.answer(
            "Похоже, это не ссылка на avito.ru. Пришли корректную ссылку на поиск.",
            reply_markup=keyboards.cancel_only(f"profile:{profile_id}:edit"),
        )
        return
    await db.update_profile(profile_id, search_url=url)
    await db.log_event("owner_action", profile_id, "url_updated")
    await state.clear()
    profile = await db.get_profile(profile_id)
    found_today = await db.count_notifications_today(profile_id)
    await message.answer(
        "Ссылка обновлена.\n\n" + formatting.profile_card_text(profile, found_today),
        reply_markup=keyboards.profile_card(profile),
    )


@router.message(EditProfileStates.waiting_name)
async def edit_receive_name(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    profile_id = data["profile_id"]
    name = (message.text or "").strip()
    if not name:
        await message.answer(
            "Название не может быть пустым. Пришли новое название.",
            reply_markup=keyboards.cancel_only(f"profile:{profile_id}:edit"),
        )
        return
    await db.update_profile(profile_id, name=name[:200])
    await db.log_event("owner_action", profile_id, "name_updated")
    await state.clear()
    profile = await db.get_profile(profile_id)
    found_today = await db.count_notifications_today(profile_id)
    await message.answer(
        "Название обновлено.\n\n" + formatting.profile_card_text(profile, found_today),
        reply_markup=keyboards.profile_card(profile),
    )


@router.message(EditProfileStates.waiting_price_ceiling)
async def edit_receive_price(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    profile_id = data["profile_id"]
    try:
        price = parse_optional_price(message.text or "")
    except ValueError:
        await message.answer(
            "Не понял число. Пришли максимальную цену цифрами или «пропустить».",
            reply_markup=keyboards.cancel_only(f"profile:{profile_id}:edit"),
        )
        return
    await db.update_profile(profile_id, price_ceiling=price)
    await db.log_event("owner_action", profile_id, "price_ceiling_updated")
    await state.clear()
    profile = await db.get_profile(profile_id)
    found_today = await db.count_notifications_today(profile_id)
    await message.answer(
        "Максимальная цена обновлена.\n\n" + formatting.profile_card_text(profile, found_today),
        reply_markup=keyboards.profile_card(profile),
    )


@router.message(EditProfileStates.waiting_stop_words)
async def edit_receive_stopwords(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    profile_id = data["profile_id"]
    stop_words = parse_stop_words(message.text or "")
    await db.update_profile(profile_id, stop_words=stop_words)
    await db.log_event("owner_action", profile_id, "stop_words_updated")
    await state.clear()
    profile = await db.get_profile(profile_id)
    found_today = await db.count_notifications_today(profile_id)
    await message.answer(
        "Стоп-слова обновлены.\n\n" + formatting.profile_card_text(profile, found_today),
        reply_markup=keyboards.profile_card(profile),
    )


# ---------------------------------------------------------------------------
# ➕ Добавить товар
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "main:add")
async def cb_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddProfileStates.waiting_url)
    await cb.message.edit_text(
        "Пришли ссылку на поиск Avito с уже настроенными фильтрами.",
        reply_markup=keyboards.cancel_only("add:cancel"),
    )
    await cb.answer()


@router.callback_query(F.data == "add:cancel")
async def cb_add_cancel(cb: CallbackQuery, state: FSMContext, db: Database, settings_cache: SettingsCache) -> None:
    await state.clear()
    await cb.answer("Добавление отменено")
    await edit_main_menu(cb, db, settings_cache)


@router.message(AddProfileStates.waiting_url)
async def add_receive_url(message: Message, state: FSMContext) -> None:
    url = validate_search_url(message.text or "")
    if url is None:
        await message.answer(
            "Похоже, это не ссылка на avito.ru. Пришли корректную ссылку на поиск.",
            reply_markup=keyboards.cancel_only("add:cancel"),
        )
        return
    await state.update_data(search_url=url)
    await state.set_state(AddProfileStates.waiting_name)
    await message.answer("Как назвать этот товар?", reply_markup=keyboards.cancel_only("add:cancel"))


@router.message(AddProfileStates.waiting_name)
async def add_receive_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Как назвать этот товар?", reply_markup=keyboards.cancel_only("add:cancel"))
        return
    await state.update_data(name=name[:200])
    await state.set_state(AddProfileStates.waiting_price_ceiling)
    await message.answer(
        "Максимальная цена для пометки «хорошая цена»? (число или «пропустить»)",
        reply_markup=keyboards.cancel_only("add:cancel"),
    )


@router.message(AddProfileStates.waiting_price_ceiling)
async def add_receive_price(message: Message, state: FSMContext) -> None:
    try:
        price = parse_optional_price(message.text or "")
    except ValueError:
        await message.answer(
            "Не понял число. Пришли максимальную цену цифрами или «пропустить».",
            reply_markup=keyboards.cancel_only("add:cancel"),
        )
        return
    await state.update_data(price_ceiling=price)
    await state.set_state(AddProfileStates.waiting_stop_words)
    await message.answer("Стоп-слова через запятую? (или «пропустить»)", reply_markup=keyboards.cancel_only("add:cancel"))


@router.message(AddProfileStates.waiting_stop_words)
async def add_receive_stopwords(message: Message, state: FSMContext, db: Database, scheduler: Scheduler) -> None:
    stop_words = parse_stop_words(message.text or "")
    await state.update_data(stop_words=stop_words)
    data = await state.get_data()
    active_count = await db.count_active_profiles()
    freq_hint = formatting.frequency_estimate_text(
        active_count + 1, (await db.get_settings()).request_budget_seconds
    )
    text = formatting.add_confirm_text(
        data["name"], data["search_url"], data.get("price_ceiling"), stop_words, freq_hint
    )
    await message.answer(text, reply_markup=keyboards.add_confirm())


@router.callback_query(F.data == "add:confirm", AddProfileStates.waiting_stop_words)
async def cb_add_confirm(cb: CallbackQuery, state: FSMContext, db: Database, scheduler: Scheduler) -> None:
    data = await state.get_data()
    profile_id = await db.add_profile(
        name=data["name"],
        search_url=data["search_url"],
        price_ceiling=data.get("price_ceiling"),
        stop_words=data.get("stop_words", []),
    )
    await state.clear()
    scheduler.request_check_now(profile_id)
    await cb.message.edit_text(
        f'Товар «{escape_html(data["name"])}» сохранён. Скоро проведу первую проверку и пришлю сводку.'
    )
    await cb.answer("Сохранено")


# ---------------------------------------------------------------------------
# ⚙️ Настройки
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "main:settings")
async def cb_settings(cb: CallbackQuery, state: FSMContext, settings_cache: SettingsCache) -> None:
    await state.clear()
    await cb.message.edit_text("⚙️ Настройки:", reply_markup=keyboards.settings_menu(settings_cache.get()))
    await cb.answer()


@router.callback_query(F.data == "settings:digest_toggle")
async def cb_settings_digest_toggle(cb: CallbackQuery, settings_cache: SettingsCache, db: Database) -> None:
    settings = settings_cache.get()
    await settings_cache.update(digest_mode=not settings.digest_mode)
    await db.log_event("owner_action", None, f"digest_mode:{not settings.digest_mode}")
    await cb.message.edit_text("⚙️ Настройки:", reply_markup=keyboards.settings_menu(settings_cache.get()))
    await cb.answer()


@router.callback_query(F.data == "settings:digest_interval")
async def cb_settings_digest_interval(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_digest_interval)
    await cb.message.edit_text(
        "Через сколько минут группировать уведомления? Пришли число.",
        reply_markup=keyboards.cancel_only("main:settings"),
    )
    await cb.answer()


@router.message(SettingsStates.waiting_digest_interval)
async def settings_receive_digest_interval(message: Message, state: FSMContext, settings_cache: SettingsCache, db: Database) -> None:
    digits = re.sub(r"[^\d]", "", message.text or "")
    if not digits or int(digits) <= 0:
        await message.answer("Пришли положительное число минут.", reply_markup=keyboards.cancel_only("main:settings"))
        return
    await settings_cache.update(digest_interval_minutes=int(digits))
    await db.log_event("owner_action", None, f"digest_interval:{digits}")
    await state.clear()
    await message.answer("⚙️ Настройки:", reply_markup=keyboards.settings_menu(settings_cache.get()))


@router.callback_query(F.data == "settings:quiet_toggle")
async def cb_settings_quiet_toggle(cb: CallbackQuery, settings_cache: SettingsCache, db: Database) -> None:
    settings = settings_cache.get()
    await settings_cache.update(quiet_hours_enabled=not settings.quiet_hours_enabled)
    await db.log_event("owner_action", None, f"quiet_hours_enabled:{not settings.quiet_hours_enabled}")
    await cb.message.edit_text("⚙️ Настройки:", reply_markup=keyboards.settings_menu(settings_cache.get()))
    await cb.answer()


@router.callback_query(F.data == "settings:quiet_range")
async def cb_settings_quiet_range(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_quiet_range)
    await cb.message.edit_text(
        "Пришли диапазон тихих часов в формате ЧЧ:ММ-ЧЧ:ММ, например 00:00-06:00.",
        reply_markup=keyboards.cancel_only("main:settings"),
    )
    await cb.answer()


@router.message(SettingsStates.waiting_quiet_range)
async def settings_receive_quiet_range(message: Message, state: FSMContext, settings_cache: SettingsCache, db: Database) -> None:
    parsed = validate_quiet_range(message.text or "")
    if parsed is None:
        await message.answer(
            "Не понял формат. Пришли диапазон как 00:00-06:00.", reply_markup=keyboards.cancel_only("main:settings")
        )
        return
    start, end = parsed
    await settings_cache.update(quiet_hours_start=start, quiet_hours_end=end)
    await db.log_event("owner_action", None, f"quiet_hours_range:{start}-{end}")
    await state.clear()
    await message.answer("⚙️ Настройки:", reply_markup=keyboards.settings_menu(settings_cache.get()))


@router.callback_query(F.data == "settings:advanced")
async def cb_settings_advanced(cb: CallbackQuery, state: FSMContext, settings_cache: SettingsCache) -> None:
    # Это ещё и цель кнопки «Отмена» для запроса бюджета запросов — без
    # сброса состояния следующее случайное текстовое сообщение молча
    # проглатывалось бы как ответ на уже отменённый вопрос.
    await state.clear()
    await cb.message.edit_text(
        "⚙️ Дополнительные настройки:", reply_markup=keyboards.settings_advanced_menu(settings_cache.get())
    )
    await cb.answer()


@router.callback_query(F.data == "settings:budget")
async def cb_settings_budget(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_budget_seconds)
    await cb.message.edit_text(
        "Минимальный интервал между проверками (в секундах, общий бюджет на все товары)? Пришли число.\n"
        "Ориентир: 60–90 секунд.",
        reply_markup=keyboards.cancel_only("settings:advanced"),
    )
    await cb.answer()


@router.message(SettingsStates.waiting_budget_seconds)
async def settings_receive_budget(message: Message, state: FSMContext, settings_cache: SettingsCache, db: Database) -> None:
    digits = re.sub(r"[^\d]", "", message.text or "")
    if not digits or int(digits) < 10:
        await message.answer(
            "Пришли число секунд, не меньше 10.", reply_markup=keyboards.cancel_only("settings:advanced")
        )
        return
    await settings_cache.update(request_budget_seconds=int(digits))
    await db.log_event("owner_action", None, f"request_budget_seconds:{digits}")
    await state.clear()
    await message.answer(
        "⚙️ Дополнительные настройки:", reply_markup=keyboards.settings_advanced_menu(settings_cache.get())
    )


# ---------------------------------------------------------------------------
# 📊 Статистика
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "main:stats")
async def cb_stats(cb: CallbackQuery, state: FSMContext, db: Database, scheduler: Scheduler) -> None:
    await state.clear()
    profiles = [p for p in await db.list_profiles() if not p.paused]
    rows = []
    for p in profiles:
        found_today = await db.count_notifications_today(p.id)
        if p.last_checked_at:
            interval = scheduler.effective_interval_estimate_seconds(len(profiles))
            # last_checked_at хранится как наивный UTC — явно помечаем tz,
            # прежде чем переводить в локальное время для отображения
            last_checked_utc = p.last_checked_at.replace(tzinfo=timezone.utc)
            next_check_utc = last_checked_utc + timedelta(seconds=interval)
            next_check_str = next_check_utc.astimezone().strftime("%H:%M")
        else:
            next_check_str = "скоро"
        rows.append(
            {
                "name": p.name,
                "found_today": found_today,
                "last_checked": p.last_checked_at,
                "next_check": next_check_str,
            }
        )
    error_count = await db.count_events_last_24h(("error",))
    captcha_count = await db.count_events_last_24h(("captcha",))
    started_at = await db.get_started_at()
    text = formatting.stats_text(rows, error_count, captcha_count, started_at)
    await cb.message.edit_text(text, reply_markup=keyboards.back_to_main())
    await cb.answer()
