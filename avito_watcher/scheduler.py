"""Планировщик: общий бюджет запросов вместо линейного роста частоты (п.7 ТЗ).

Все активные профили стоят в круговой очереди (round-robin). Между двумя
любыми последовательными проверками (независимо от профиля) выдерживается
`request_budget_seconds` (± джиттер, п.8). Эффективная частота проверки
конкретного профиля = request_budget_seconds × количество активных
профилей. Ручное «🔍 Проверить сейчас» — точечное исключение из очереди,
но тоже уважает минимальный интервал.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, time as dtime
from typing import Awaitable, Callable

from avito_watcher.db import Database
from avito_watcher.models import GlobalSettings

JITTER_RATIO = 0.25
MIN_INTERVAL_FLOOR = 5.0


def _parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(hour=int(hh), minute=int(mm))


def is_within_quiet_hours(settings: GlobalSettings, now: datetime | None = None) -> bool:
    if not settings.quiet_hours_enabled:
        return False
    now = now or datetime.now()
    current = now.time()
    start = _parse_hhmm(settings.quiet_hours_start)
    end = _parse_hhmm(settings.quiet_hours_end)
    if start <= end:
        return start <= current < end
    # диапазон через полночь, например 23:00–06:00
    return current >= start or current < end


class Scheduler:
    """Держит очередь активных профилей и тайминг между проверками."""

    def __init__(self, db: Database, settings_provider: Callable[[], GlobalSettings]):
        self.db = db
        self.settings_provider = settings_provider
        self._priority: "asyncio.Queue[int]" = asyncio.Queue()
        self._rr_cursor = 0
        self._last_check_monotonic: float = -1e9  # позволяет первой проверке пройти сразу

    def request_check_now(self, profile_id: int) -> None:
        self._priority.put_nowait(profile_id)

    def current_budget_seconds(self, now: datetime | None = None) -> float:
        settings = self.settings_provider()
        base = float(settings.request_budget_seconds)
        if is_within_quiet_hours(settings, now):
            base *= settings.quiet_hours_multiplier
        jitter = base * random.uniform(-JITTER_RATIO, JITTER_RATIO)
        return max(MIN_INTERVAL_FLOOR, base + jitter)

    def seconds_until_next_slot(self) -> float:
        budget = self.current_budget_seconds()
        elapsed = time.monotonic() - self._last_check_monotonic
        return max(0.0, budget - elapsed)

    async def wait_for_slot(self) -> None:
        remaining = self.seconds_until_next_slot()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def get_next_profile_id(self) -> int | None:
        try:
            candidate = self._priority.get_nowait()
            profile = await self.db.get_profile(candidate)
            if profile is not None:
                return candidate
        except asyncio.QueueEmpty:
            pass

        active_ids = await self.db.list_active_profile_ids()
        if not active_ids:
            return None
        pid = active_ids[self._rr_cursor % len(active_ids)]
        self._rr_cursor += 1
        return pid

    def mark_checked(self) -> None:
        self._last_check_monotonic = time.monotonic()

    def effective_interval_estimate_seconds(self, active_profile_count: int) -> float:
        """Средняя частота проверки одного конкретного профиля (п.7)."""
        settings = self.settings_provider()
        return settings.request_budget_seconds * max(1, active_profile_count)
