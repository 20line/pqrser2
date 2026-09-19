"""Текстовые шаблоны для сообщений бота (п.9, п.10 ТЗ)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Sequence

from avito_watcher.models import Profile
from avito_watcher.notifications import format_price
from avito_watcher.textutils import escape_html


def _plural_tovarov(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "ов"
    last = n % 10
    if last == 1:
        return ""
    if 2 <= last <= 4:
        return "а"
    return "ов"


def frequency_estimate_text(active_count: int, budget_seconds: int) -> str:
    per_profile_seconds = budget_seconds * max(1, active_count)
    minutes = max(1, round(per_profile_seconds / 60))
    return (
        f"Сейчас у вас {active_count} активных товар{_plural_tovarov(active_count)}, "
        f"каждый будет проверяться в среднем раз в ~{minutes} мин."
    )


def profile_card_text(profile: Profile, found_today: int) -> str:
    lines = [
        f"<b>{escape_html(profile.name)}</b>",
        "🟢 активен" if not profile.paused else "⏸ на паузе",
        f"🔗 {escape_html(profile.search_url)}",
    ]
    if profile.price_ceiling:
        lines.append(f"💰 Хорошая цена: до {format_price(profile.price_ceiling)}")
    if profile.stop_words:
        lines.append(f"🚫 Стоп-слова: {escape_html(', '.join(profile.stop_words))}")
    lines.append(f"📊 Сегодня найдено: {found_today}")
    if profile.last_checked_at:
        lines.append(f"🕐 Последняя проверка: {profile.last_checked_at.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        lines.append("🕐 Ещё не проверялся")
    return "\n".join(lines)


def add_confirm_text(name: str, url: str, price_ceiling: int | None, stop_words: list[str], freq_hint: str) -> str:
    lines = [
        "Проверь данные перед сохранением:",
        f"Название: {escape_html(name)}",
        f"Ссылка: {escape_html(url)}",
        f"Хорошая цена: {format_price(price_ceiling) if price_ceiling else 'не задана'}",
        f"Стоп-слова: {escape_html(', '.join(stop_words)) if stop_words else 'нет'}",
        "",
        freq_hint,
    ]
    return "\n".join(lines)


def history_text(profile_name: str, rows: Sequence) -> str:
    profile_name = escape_html(profile_name)
    if not rows:
        return f"По товару «{profile_name}» пока нет уведомлений."
    lines = [f"Последние найденные по «{profile_name}»:"]
    for r in rows:
        icon = "🆕" if r["kind"] == "new" else "📉"
        lines.append(f"{icon} {escape_html(r['title'])} — {format_price(r['price'])}\n{escape_html(r['url'])}")
    return "\n\n".join(lines) if len(lines) > 1 else lines[0]


def stats_text(
    rows: Iterable[dict],
    error_count_24h: int,
    captcha_count_24h: int,
    started_at: datetime,
) -> str:
    lines = ["<b>📊 Статистика</b>", ""]
    rows = list(rows)
    if not rows:
        lines.append("Нет активных товаров.")
    for r in rows:
        last_checked = r["last_checked"].strftime("%H:%M") if r["last_checked"] else "ещё не было"
        lines.append(
            f"• {escape_html(r['name'])}: сегодня найдено {r['found_today']}, "
            f"последняя проверка {last_checked}, следующая ~{r['next_check']}"
        )
    lines.append("")
    lines.append(f"Ошибок за 24ч: {error_count_24h}")
    lines.append(f"Капч/блокировок за 24ч: {captcha_count_24h}")
    lines.append(f"Аптайм: {format_uptime(datetime.utcnow() - started_at)}")
    return "\n".join(lines)


def format_uptime(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours or days:
        parts.append(f"{hours} ч.")
    parts.append(f"{minutes} мин.")
    return " ".join(parts)
