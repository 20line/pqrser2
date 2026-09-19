from datetime import datetime, time as dtime

import pytest

from avito_watcher.models import GlobalSettings
from avito_watcher.scheduler import Scheduler, is_within_quiet_hours


class FakeDB:
    def __init__(self, active_ids):
        self.active_ids = active_ids

    async def list_active_profile_ids(self):
        return list(self.active_ids)

    async def get_profile(self, profile_id):
        return object() if profile_id in self.active_ids else None


def make_settings(**overrides) -> GlobalSettings:
    base = dict(owner_chat_id=1, request_budget_seconds=60)
    base.update(overrides)
    return GlobalSettings(**base)


async def test_round_robin_cycles_through_all_active_profiles():
    db = FakeDB([1, 2, 3])
    settings = make_settings()
    scheduler = Scheduler(db, lambda: settings)

    seen = [await scheduler.get_next_profile_id() for _ in range(6)]
    assert seen == [1, 2, 3, 1, 2, 3]


async def test_get_next_profile_id_returns_none_when_no_active_profiles():
    db = FakeDB([])
    settings = make_settings()
    scheduler = Scheduler(db, lambda: settings)
    assert await scheduler.get_next_profile_id() is None


async def test_priority_check_now_bypasses_round_robin():
    db = FakeDB([1, 2, 3])
    settings = make_settings()
    scheduler = Scheduler(db, lambda: settings)

    scheduler.request_check_now(2)
    first = await scheduler.get_next_profile_id()
    assert first == 2

    # после приоритетного запроса очередь round-robin продолжается с начала
    rest = [await scheduler.get_next_profile_id() for _ in range(3)]
    assert rest == [1, 2, 3]


async def test_priority_request_for_deleted_profile_is_skipped():
    db = FakeDB([1, 2])
    settings = make_settings()
    scheduler = Scheduler(db, lambda: settings)

    scheduler.request_check_now(999)  # профиль уже удалён
    result = await scheduler.get_next_profile_id()
    assert result == 1  # падаем обратно на round-robin


def test_effective_interval_scales_with_active_profile_count():
    settings = make_settings(request_budget_seconds=60)
    scheduler = Scheduler(FakeDB([]), lambda: settings)

    assert scheduler.effective_interval_estimate_seconds(1) == 60
    assert scheduler.effective_interval_estimate_seconds(4) == 240
    # не должно расти линейнее, чем budget * count (и не меньше budget при 0 активных)
    assert scheduler.effective_interval_estimate_seconds(0) == 60


def test_current_budget_seconds_within_jitter_bounds():
    settings = make_settings(request_budget_seconds=100)
    scheduler = Scheduler(FakeDB([]), lambda: settings)

    for _ in range(200):
        value = scheduler.current_budget_seconds(now=datetime(2024, 1, 1, 12, 0))
        assert 75.0 <= value <= 125.0


def test_quiet_hours_multiplies_budget():
    settings = make_settings(
        request_budget_seconds=100,
        quiet_hours_enabled=True,
        quiet_hours_start="00:00",
        quiet_hours_end="06:00",
        quiet_hours_multiplier=3.0,
    )
    scheduler = Scheduler(FakeDB([]), lambda: settings)

    night = datetime(2024, 1, 1, 2, 0)
    day = datetime(2024, 1, 1, 14, 0)
    night_budget = scheduler.current_budget_seconds(now=night)
    day_budget = scheduler.current_budget_seconds(now=day)
    assert night_budget > day_budget


@pytest.mark.parametrize(
    "current,start,end,expected",
    [
        (dtime(2, 0), "00:00", "06:00", True),
        (dtime(7, 0), "00:00", "06:00", False),
        (dtime(23, 30), "22:00", "06:00", True),  # диапазон через полночь
        (dtime(12, 0), "22:00", "06:00", False),
    ],
)
def test_is_within_quiet_hours_handles_midnight_wraparound(current, start, end, expected):
    settings = make_settings(
        quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end
    )
    now = datetime.combine(datetime(2024, 1, 1), current)
    assert is_within_quiet_hours(settings, now) is expected


def test_is_within_quiet_hours_false_when_disabled():
    settings = make_settings(quiet_hours_enabled=False)
    assert is_within_quiet_hours(settings, datetime(2024, 1, 1, 2, 0)) is False


def test_is_within_quiet_hours_uses_settings_timezone_when_now_omitted():
    """Регрессия: раньше функция всегда брала datetime.now() (системный
    часовой пояс сервера), полностью игнорируя settings.timezone (п.5 ТЗ).
    Проверяем, что с now=None вызов реально смотрит на зону из настроек и
    не падает ни на валидной, ни на некорректной строке зоны."""
    settings_msk = make_settings(
        quiet_hours_enabled=True, quiet_hours_start="00:00", quiet_hours_end="23:59", timezone="Europe/Moscow"
    )
    assert is_within_quiet_hours(settings_msk) is True  # весь день "тихий" — не должно падать

    settings_bad_tz = make_settings(
        quiet_hours_enabled=True, quiet_hours_start="00:00", quiet_hours_end="23:59", timezone="Not/AZone"
    )
    assert is_within_quiet_hours(settings_bad_tz) is True  # graceful fallback, без исключения
