"""Форматирование и отправка уведомлений, включая digest-режим (п.9, п.10 ТЗ)."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Protocol

from avito_watcher.db import Database
from avito_watcher.models import GlobalSettings, ListingCard, NotificationEvent, Profile
from avito_watcher.textutils import escape_html


class SendMessageFn(Protocol):
    async def __call__(self, chat_id: int, text: str, **kwargs) -> None: ...


TELEGRAM_MAX_MESSAGE_LENGTH = 4096
DIGEST_SEPARATOR = "\n\n---\n\n"


def format_price(price: Optional[int]) -> str:
    if price is None:
        return "цена не указана"
    return f"{price:,}".replace(",", " ") + " ₽"


def build_new_listing_event(profile: Profile, card: ListingCard) -> NotificationEvent:
    good_price = profile.price_ceiling is not None and card.price is not None and card.price < profile.price_ceiling
    return NotificationEvent(
        kind="new",
        profile_id=profile.id,
        profile_name=profile.name,
        title=card.title,
        url=card.url,
        price=card.price,
        location=card.location,
        good_price=good_price,
    )


def build_price_drop_event(profile: Profile, card: ListingCard, old_price: Optional[int]) -> NotificationEvent:
    return NotificationEvent(
        kind="price_drop",
        profile_id=profile.id,
        profile_name=profile.name,
        title=card.title,
        url=card.url,
        price=card.price,
        old_price=old_price,
        location=card.location,
    )


def format_event(ev: NotificationEvent) -> str:
    profile_name = escape_html(ev.profile_name)
    title = escape_html(ev.title)
    url = escape_html(ev.url)
    if ev.kind == "new":
        lines = [f"🆕 {profile_name}", title]
        price_line = f"💰 {format_price(ev.price)}"
        if ev.good_price:
            price_line += " 🔥 хорошая цена"
        lines.append(price_line)
        if ev.location:
            lines.append(f"📍 {escape_html(ev.location)}")
        lines.append(url)
        return "\n".join(lines)
    elif ev.kind == "price_drop":
        lines = [
            f"📉 {profile_name}",
            title,
            f"Было: {format_price(ev.old_price)} → Стало: {format_price(ev.price)}",
            url,
        ]
        return "\n".join(lines)
    raise ValueError(f"Неизвестный тип уведомления: {ev.kind}")


class Notifier:
    def __init__(
        self,
        send_message: SendMessageFn,
        db: Database,
        settings_provider: Callable[[], GlobalSettings],
        logger: Optional[logging.Logger] = None,
    ):
        self._send_message = send_message
        self.db = db
        self.settings_provider = settings_provider
        self.logger = logger or logging.getLogger("avito_watcher")
        self._digest_queue: list[NotificationEvent] = []
        self._lock = asyncio.Lock()

    async def _owner_chat_id(self) -> int:
        return self.settings_provider().owner_chat_id

    async def enqueue(self, ev: NotificationEvent) -> None:
        await self.db.log_notification(
            profile_id=ev.profile_id,
            kind=ev.kind,
            title=ev.title,
            price=ev.price,
            old_price=ev.old_price,
            url=ev.url,
            location=ev.location,
        )
        settings = self.settings_provider()
        if settings.digest_mode:
            async with self._lock:
                self._digest_queue.append(ev)
        else:
            await self._send_text(format_event(ev))

    async def _send_text(self, text: str) -> bool:
        owner_chat_id = await self._owner_chat_id()
        try:
            await self._send_message(chat_id=owner_chat_id, text=text)
            return True
        except Exception:
            self.logger.exception("Не удалось отправить сообщение владельцу")
            return False

    async def send_alert(self, text: str) -> None:
        await self._send_text(text)

    async def notify_profile_added_summary(self, profile: Profile, listings_count: int) -> None:
        text = (
            f'Добавлен товар "{escape_html(profile.name)}", в выдаче сейчас {listings_count} '
            f"объявлени{_plural_ya(listings_count)}, буду присылать только новые."
        )
        await self._send_text(text)

    async def flush_digest_if_due(self) -> None:
        """Вызывается периодически внешним таймером; сам решает, отправлять ли.

        Разбивает накопленные уведомления на части под лимит длины
        сообщения Telegram (4096 символов) и не теряет недоставленное:
        при сбое отправки недошедший остаток возвращается в очередь для
        следующей попытки, а не молча отбрасывается.
        """
        async with self._lock:
            if not self._digest_queue:
                return
            batch = self._digest_queue
            self._digest_queue = []

        chunks: list[list[NotificationEvent]] = [[]]
        chunk_len = 0
        for ev in batch:
            piece_len = len(format_event(ev)) + len(DIGEST_SEPARATOR)
            if chunks[-1] and chunk_len + piece_len > TELEGRAM_MAX_MESSAGE_LENGTH:
                chunks.append([])
                chunk_len = 0
            chunks[-1].append(ev)
            chunk_len += piece_len

        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            text = DIGEST_SEPARATOR.join(format_event(ev) for ev in chunk)
            if not await self._send_text(text):
                leftover = [ev for remaining_chunk in chunks[i:] for ev in remaining_chunk]
                async with self._lock:
                    self._digest_queue = leftover + self._digest_queue
                break

    async def digest_flush_loop(self) -> None:
        """Фоновая задача: раз в digest_interval_minutes отправляет накопленное."""
        while True:
            settings = self.settings_provider()
            if settings.digest_mode:
                interval_seconds = max(60, settings.digest_interval_minutes * 60)
            else:
                interval_seconds = 30  # в мгновенном режиме очередь пуста, просто ждём дешёво
            await asyncio.sleep(interval_seconds)
            if self.settings_provider().digest_mode:
                await self.flush_digest_if_due()

    def pending_digest_count(self) -> int:
        return len(self._digest_queue)


def _plural_ya(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "й"
    last = n % 10
    if last == 1:
        return "е"
    if 2 <= last <= 4:
        return "я"
    return "й"
