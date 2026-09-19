"""Общие текстовые утилиты."""

from __future__ import annotations

import html


def escape_html(text) -> str:
    """Экранирует &, <, > для безопасной вставки в HTML-разметку сообщений
    Telegram (бот работает с parse_mode=HTML по умолчанию).

    Обязательно для любого динамического текста: названий профилей,
    заголовков объявлений, ссылок — поисковые ссылки Avito почти всегда
    содержат `&` между параметрами фильтров, и без экранирования Telegram
    отклоняет такое сообщение целиком ("can't parse entities").
    """
    return html.escape(str(text), quote=False)
