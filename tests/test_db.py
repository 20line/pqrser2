from pathlib import Path

import pytest

from avito_watcher.db import Database


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.ensure_global_settings(owner_chat_id=42)
    yield database
    await database.close()


async def test_ensure_global_settings_seeds_owner(db: Database):
    settings = await db.get_settings()
    assert settings.owner_chat_id == 42
    assert settings.paused_all is False


async def test_add_and_get_profile_roundtrip(db: Database):
    profile_id = await db.add_profile(
        name="iPhone 13", search_url="https://www.avito.ru/search1", price_ceiling=40000,
        stop_words=["битый", "муляж"],
    )
    profile = await db.get_profile(profile_id)
    assert profile.name == "iPhone 13"
    assert profile.price_ceiling == 40000
    assert profile.stop_words == ["битый", "муляж"]
    assert profile.paused is False
    assert profile.last_checked_at is None
    assert profile.is_first_run is True


async def test_touch_profile_checked_clears_first_run(db: Database):
    profile_id = await db.add_profile("X", "https://www.avito.ru/s", None, [])
    await db.touch_profile_checked(profile_id)
    profile = await db.get_profile(profile_id)
    assert profile.is_first_run is False


async def test_set_profile_paused_excludes_from_active_ids(db: Database):
    p1 = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    p2 = await db.add_profile("B", "https://www.avito.ru/2", None, [])
    await db.set_profile_paused(p1, True)
    active = await db.list_active_profile_ids()
    assert active == [p2]
    assert await db.count_active_profiles() == 1


async def test_update_profile_partial_fields(db: Database):
    pid = await db.add_profile("A", "https://www.avito.ru/1", 1000, ["a"])
    await db.update_profile(pid, name="A2")
    profile = await db.get_profile(pid)
    assert profile.name == "A2"
    assert profile.price_ceiling == 1000  # не тронуто

    await db.update_profile(pid, price_ceiling=None)
    profile = await db.get_profile(pid)
    assert profile.price_ceiling is None


async def test_delete_profile_cascades_seen_listings(db: Database):
    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    await db.upsert_seen_listing(pid, "123", 1000)
    await db.delete_profile(pid)
    assert await db.get_profile(pid) is None


async def test_seen_listings_upsert_updates_price(db: Database):
    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    await db.upsert_seen_listing(pid, "123", 5000)
    await db.upsert_seen_listing(pid, "123", 4500)
    seen = await db.get_seen_listings(pid)
    assert seen["123"].last_price == 4500


async def test_update_settings_partial(db: Database):
    await db.update_settings(digest_mode=True, digest_interval_minutes=20)
    settings = await db.get_settings()
    assert settings.digest_mode is True
    assert settings.digest_interval_minutes == 20
    assert settings.request_budget_seconds == 75  # дефолт не тронут


async def test_captcha_state_roundtrip(db: Database):
    from datetime import datetime, timedelta

    resume_at = datetime.utcnow() + timedelta(minutes=15)
    await db.set_captcha_state(active=True, resume_at=resume_at, strikes=2, last_captcha_at=datetime.utcnow())
    state = await db.get_captcha_state()
    assert state.active is True
    assert state.strikes == 2
    assert state.resume_at is not None


async def test_notifications_log_and_recent(db: Database):
    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    await db.log_notification(pid, "new", "Title", 1000, None, "https://x", "Москва")
    rows = await db.get_recent_notifications(pid, limit=5)
    assert len(rows) == 1
    assert rows[0]["title"] == "Title"
    assert await db.count_notifications_today(pid) == 1


async def test_events_log_count_last_24h(db: Database):
    await db.log_event("error", None, "boom")
    await db.log_event("captcha", None, "blocked")
    assert await db.count_events_last_24h(("error",)) == 1
    assert await db.count_events_last_24h(("error", "captcha")) == 2


async def test_events_log_excludes_events_older_than_24h(db: Database):
    """Регрессия: сравнение хранимой метки времени с datetime('now', '-1 day')
    в SQL раньше давало неверный результат из-за разных разделителей даты/
    времени ('T' у Python isoformat() против пробела у SQLite datetime()) —
    события ровно на границе суток внутри «сегодня», но раньше порога 24ч,
    ошибочно засчитывались. Проверяем обе стороны границы напрямую."""
    from datetime import datetime, timedelta

    old_ts = (datetime.utcnow() - timedelta(hours=30)).isoformat(sep=" ", timespec="seconds")
    recent_ts = (datetime.utcnow() - timedelta(hours=1)).isoformat(sep=" ", timespec="seconds")
    await db.conn.execute(
        "INSERT INTO events_log (event_type, profile_id, detail, ts) VALUES (?, NULL, 'old', ?)",
        ("error", old_ts),
    )
    await db.conn.execute(
        "INSERT INTO events_log (event_type, profile_id, detail, ts) VALUES (?, NULL, 'recent', ?)",
        ("error", recent_ts),
    )
    await db.conn.commit()

    assert await db.count_events_last_24h(("error",)) == 1


async def test_layout_broken_flag(db: Database):
    assert await db.get_layout_broken() is False
    await db.set_layout_broken(True)
    assert await db.get_layout_broken() is True
