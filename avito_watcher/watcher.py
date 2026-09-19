"""Watcher Engine — ядро: держит Playwright-браузер, прогоняет профили,
парсит DOM, определяет новые объявления/изменения цены (п.6, п.8, п.11 ТЗ).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from avito_watcher import captcha, notifications, parser
from avito_watcher.db import Database
from avito_watcher.logging_setup import dump_html_for_debug
from avito_watcher.models import GlobalSettings, Profile
from avito_watcher.notifications import Notifier
from avito_watcher.scheduler import Scheduler

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

# Пороговые значения — «на усмотрение разработчика» (п.14 ТЗ), разумные дефолты.
ERROR_ALERT_THRESHOLD = 4
CAPTCHA_BACKOFF_BASE_SECONDS = 15 * 60
CAPTCHA_BACKOFF_MAX_SECONDS = 6 * 60 * 60
CAPTCHA_BACKOFF_MULTIPLIER = 2
CAPTCHA_STRIKE_RESET_AFTER_SECONDS = 6 * 60 * 60
IDLE_POLL_SECONDS = 5.0


class CaptchaDetected(Exception):
    pass


class WatcherEngine:
    def __init__(
        self,
        db: Database,
        scheduler: Scheduler,
        notifier: Notifier,
        settings_provider: Callable[[], GlobalSettings],
        browser_profile_dir: Path,
        log_dir: Path,
        logger: Optional[logging.Logger] = None,
    ):
        self.db = db
        self.scheduler = scheduler
        self.notifier = notifier
        self.settings_provider = settings_provider
        self.browser_profile_dir = browser_profile_dir
        self.log_dir = log_dir
        self.logger = logger or logging.getLogger("avito_watcher")

        self.context: Optional["BrowserContext"] = None
        self.page: Optional["Page"] = None

        self._consecutive_errors = 0
        self._error_alert_sent = False
        self._stopped = False

    # ---------- lifecycle ----------

    async def start(self, playwright: "Playwright") -> None:
        self.context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.browser_profile_dir),
            headless=False,  # п.8 ТЗ: без headless-режима
            viewport={"width": 1366, "height": 900},
            locale="ru-RU",
            timezone_id=self.settings_provider().timezone or "Europe/Moscow",
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.logger.info("Playwright persistent context запущен")

    async def stop(self) -> None:
        self._stopped = True
        if self.context is not None:
            await self.context.close()
            self.context = None
            self.page = None

    # ---------- main loop ----------

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                if await self._is_blocked_for_now():
                    await asyncio.sleep(IDLE_POLL_SECONDS)
                    continue

                await self.scheduler.wait_for_slot()
                profile_id = await self.scheduler.get_next_profile_id()
                if profile_id is None:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
                    continue

                await self._run_single_check(profile_id)
                self.scheduler.mark_checked()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Неожиданная ошибка в главном цикле Watcher Engine")
                await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _is_blocked_for_now(self) -> bool:
        settings = self.settings_provider()
        captcha_state = await self.db.get_captcha_state()
        if captcha_state.active:
            if captcha_state.resume_at and datetime.utcnow() >= captcha_state.resume_at:
                await self.resume_all(manual=False)
                return False
            return True
        return settings.paused_all

    async def _run_single_check(self, profile_id: int) -> None:
        try:
            await self.check_profile(profile_id)
        except CaptchaDetected:
            await self._handle_captcha()
        except Exception as exc:  # сетевые сбои / таймауты / прочее (п.11 ТЗ)
            await self._handle_error(profile_id, exc)

    # ---------- single check ----------

    async def check_profile(self, profile_id: int) -> None:
        profile = await self.db.get_profile(profile_id)
        if profile is None or profile.paused:
            return

        assert self.page is not None, "Watcher не инициализирован (start() не вызван)"
        self.logger.info("Проверка профиля #%s «%s»", profile.id, profile.name)

        await self.page.goto(profile.search_url, wait_until="domcontentloaded", timeout=30_000)
        await self._humanlike_behavior()

        html = await self.page.content()
        current_url = self.page.url

        if captcha.detect(html, current_url):
            raise CaptchaDetected()

        cards = parser.extract_cards(html)
        if not cards and parser.looks_like_layout_change(html):
            await self._handle_layout_change(profile, html)
            return

        await self._handle_layout_recovered_if_needed()

        cards = [c for c in cards if not parser.matches_stop_words(c, profile.stop_words)]
        seen = await self.db.get_seen_listings(profile.id)
        is_first_run = profile.is_first_run

        events = []
        for card in cards:
            prior = seen.get(card.listing_id)
            if prior is None:
                await self.db.upsert_seen_listing(profile.id, card.listing_id, card.price)
                if not is_first_run:
                    events.append(notifications.build_new_listing_event(profile, card))
            else:
                price_dropped = (
                    prior.last_price is not None
                    and card.price is not None
                    and card.price < prior.last_price
                )
                # обновляем сохранённую цену при любом изменении (и росте, и
                # падении) — иначе после роста цены дальнейшее её снижение
                # сравнивалось бы со старой заниженной отметкой и терялось
                if card.price != prior.last_price:
                    await self.db.upsert_seen_listing(
                        profile.id, card.listing_id, card.price, prior.first_seen_at
                    )
                if price_dropped and not is_first_run:
                    events.append(notifications.build_price_drop_event(profile, card, prior.last_price))

        await self.db.touch_profile_checked(profile.id)
        self._consecutive_errors = 0
        self._error_alert_sent = False

        if is_first_run:
            await self.notifier.notify_profile_added_summary(profile, len(cards))
            await self.db.log_event("owner_action", profile.id, f"first_pass_baseline:{len(cards)}")
        else:
            for ev in events:
                await self.notifier.enqueue(ev)

    # ---------- anti-detect helpers ----------

    async def _humanlike_behavior(self) -> None:
        assert self.page is not None
        scroll_steps = random.randint(1, 3)
        for _ in range(scroll_steps):
            await self.page.mouse.wheel(0, random.randint(250, 550))
            await asyncio.sleep(random.uniform(0.3, 0.9))
        await asyncio.sleep(random.uniform(1.0, 3.0))

    # ---------- captcha handling (п.8 ТЗ) ----------

    async def _handle_captcha(self) -> None:
        now = datetime.utcnow()
        state = await self.db.get_captcha_state()
        strikes = state.strikes
        if state.last_captcha_at and (now - state.last_captcha_at).total_seconds() > CAPTCHA_STRIKE_RESET_AFTER_SECONDS:
            strikes = 0
        strikes += 1
        backoff = min(
            CAPTCHA_BACKOFF_MAX_SECONDS,
            CAPTCHA_BACKOFF_BASE_SECONDS * (CAPTCHA_BACKOFF_MULTIPLIER ** (strikes - 1)),
        )
        resume_at = now + timedelta(seconds=backoff)
        await self.db.set_captcha_state(active=True, resume_at=resume_at, strikes=strikes, last_captcha_at=now)
        await self.db.log_event("captcha", None, f"strike={strikes} backoff={backoff}s")
        self.logger.warning("Обнаружена капча/блокировка Avito, ставим все проверки на паузу (strike=%s)", strikes)
        minutes = max(1, backoff // 60)
        await self.notifier.send_alert(
            "🛑 Avito просит капчу, реши вручную в открытом окне браузера.\n"
            f"Автоматические проверки возобновятся не раньше чем через ~{minutes} мин, "
            "либо нажми «▶️ Возобновить всё» в меню бота."
        )

    async def resume_all(self, manual: bool) -> None:
        await self.db.set_captcha_state(active=False, resume_at=None)
        await self.db.update_settings(paused_all=False)
        if manual:
            await self.db.log_event("owner_action", None, "manual_resume_all")
            self.logger.info("Владелец вручную возобновил все проверки")
        else:
            await self.db.log_event("captcha", None, "auto_resume_after_backoff")
            self.logger.info("Автоматическое возобновление проверок после капча-бэкоффа")
            await self.notifier.send_alert("✅ Тайм-аут истёк, возобновляю автоматические проверки.")

    async def pause_all_manual(self) -> None:
        await self.db.update_settings(paused_all=True)
        await self.db.log_event("owner_action", None, "manual_pause_all")

    # ---------- errors (п.11 ТЗ) ----------

    async def _handle_error(self, profile_id: int, exc: Exception) -> None:
        self._consecutive_errors += 1
        self.logger.warning(
            "Ошибка при проверке профиля #%s: %r (подряд: %s)", profile_id, exc, self._consecutive_errors
        )
        await self.db.log_event("error", profile_id, repr(exc))
        if self._consecutive_errors >= ERROR_ALERT_THRESHOLD and not self._error_alert_sent:
            await self.notifier.send_alert(
                f"⚠️ Подряд {self._consecutive_errors} проверки завершились ошибкой. "
                "Проверь соединение или доступность сайта — подробности в логах."
            )
            self._error_alert_sent = True

    # ---------- layout change (п.11 ТЗ) ----------

    async def _handle_layout_change(self, profile: Profile, html: str) -> None:
        already_broken = await self.db.get_layout_broken()
        dump_path = dump_html_for_debug(self.log_dir, profile.id, html)
        self.logger.error(
            "Похоже, вёрстка Avito изменилась (профиль #%s). HTML сохранён в %s", profile.id, dump_path
        )
        await self.db.log_event("layout_change", profile.id, str(dump_path))
        if not already_broken:
            await self.db.set_layout_broken(True)
            await self.notifier.send_alert(
                "⚠️ Похоже, сайт изменился, проверка не работает. HTML сохранён для разбора, чиню."
            )

    async def _handle_layout_recovered_if_needed(self) -> None:
        if await self.db.get_layout_broken():
            await self.db.set_layout_broken(False)
            await self.db.log_event("layout_recovered", None, "cards_found_again")
            await self.notifier.send_alert("✅ Похоже, парсинг Avito снова работает штатно.")
