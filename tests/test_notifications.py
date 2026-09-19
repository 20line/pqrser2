from pathlib import Path

import pytest

from avito_watcher.db import Database
from avito_watcher.models import GlobalSettings, ListingCard, Profile
from avito_watcher.notifications import (
    Notifier,
    build_new_listing_event,
    build_price_drop_event,
    format_event,
    format_price,
)


def make_profile(**overrides) -> Profile:
    base = dict(
        id=1,
        name="iPhone 13",
        search_url="https://www.avito.ru/x",
        price_ceiling=None,
        stop_words=[],
        paused=False,
        created_at=None,
        last_checked_at=None,
    )
    base.update(overrides)
    return Profile(**base)


def test_format_price_none_and_number():
    assert format_price(None) == "цена не указана"
    assert format_price(35000) == "35 000 ₽"


def test_build_new_listing_event_marks_good_price():
    profile = make_profile(price_ceiling=40000)
    card = ListingCard(listing_id="1", title="iPhone", price=35000, url="https://x", location="Москва")
    ev = build_new_listing_event(profile, card)
    assert ev.good_price is True
    assert ev.kind == "new"

    card_expensive = ListingCard(listing_id="2", title="iPhone", price=45000, url="https://x")
    ev2 = build_new_listing_event(profile, card_expensive)
    assert ev2.good_price is False


def test_format_event_new_listing_contains_expected_blocks():
    profile = make_profile(name="Профиль X", price_ceiling=40000)
    card = ListingCard(listing_id="1", title="iPhone 13", price=35000, url="https://x", location="Москва")
    ev = build_new_listing_event(profile, card)
    text = format_event(ev)
    assert text.startswith("🆕 Профиль X\niPhone 13")
    assert "🔥 хорошая цена" in text
    assert "📍 Москва" in text
    assert "https://x" in text


def test_format_event_price_drop_contains_old_and_new_price():
    profile = make_profile()
    card = ListingCard(listing_id="1", title="iPhone 13", price=30000, url="https://x")
    ev = build_price_drop_event(profile, card, old_price=35000)
    text = format_event(ev)
    assert "📉" in text
    assert "35 000" in text
    assert "30 000" in text


def test_format_event_escapes_html_special_chars_in_url_and_title():
    """Регрессия: Telegram-бот шлёт сообщения с parse_mode=HTML, а
    поисковые ссылки Avito почти всегда содержат '&' между параметрами
    фильтров — без экранирования такое сообщение целиком отклонялось бы
    Telegram API ("can't parse entities")."""
    profile = make_profile(name="Тест <script> & Co")
    card = ListingCard(
        listing_id="1",
        title="iPhone <13> & чехол",
        price=1000,
        url="https://www.avito.ru/search?a=1&b=2",
    )
    ev = build_new_listing_event(profile, card)
    text = format_event(ev)
    assert "<script>" not in text
    assert "&b=2" not in text  # сырой '&' не должен просочиться как есть
    assert "&amp;" in text
    assert "&lt;script&gt;" in text


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.ensure_global_settings(owner_chat_id=42)
    yield database
    await database.close()


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


async def test_notifier_sends_immediately_when_digest_disabled(db: Database):
    settings = GlobalSettings(owner_chat_id=42, digest_mode=False)
    recorder = Recorder()
    notifier = Notifier(recorder, db, lambda: settings)

    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    profile = make_profile(id=pid)
    card = ListingCard(listing_id="1", title="iPhone", price=1000, url="https://x")
    await notifier.enqueue(build_new_listing_event(profile, card))

    assert len(recorder.sent) == 1
    assert recorder.sent[0][0] == 42


async def test_notifier_queues_in_digest_mode_until_flush(db: Database):
    settings = GlobalSettings(owner_chat_id=42, digest_mode=True, digest_interval_minutes=15)
    recorder = Recorder()
    notifier = Notifier(recorder, db, lambda: settings)

    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    profile = make_profile(id=pid)
    card1 = ListingCard(listing_id="1", title="iPhone A", price=1000, url="https://a")
    card2 = ListingCard(listing_id="2", title="iPhone B", price=2000, url="https://b")
    await notifier.enqueue(build_new_listing_event(profile, card1))
    await notifier.enqueue(build_new_listing_event(profile, card2))

    assert recorder.sent == []  # ничего не отправлено сразу
    assert notifier.pending_digest_count() == 2

    await notifier.flush_digest_if_due()
    assert len(recorder.sent) == 1
    combined_text = recorder.sent[0][1]
    assert "iPhone A" in combined_text and "iPhone B" in combined_text
    assert notifier.pending_digest_count() == 0


async def test_digest_flush_splits_into_chunks_under_telegram_limit(db: Database):
    settings = GlobalSettings(owner_chat_id=42, digest_mode=True, digest_interval_minutes=15)
    recorder = Recorder()
    notifier = Notifier(recorder, db, lambda: settings)

    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    profile = make_profile(id=pid)
    # У каждого события длинный заголовок, чтобы гарантированно перевалить
    # за лимит Telegram (4096 символов) суммарно и потребовать несколько частей.
    for i in range(40):
        card = ListingCard(
            listing_id=str(i),
            title="X" * 150,
            price=1000 + i,
            url=f"https://x/{i}",
        )
        await notifier.enqueue(build_new_listing_event(profile, card))

    await notifier.flush_digest_if_due()

    assert len(recorder.sent) > 1  # не влезло в одно сообщение
    for _, text in recorder.sent:
        assert len(text) <= 4096
    assert notifier.pending_digest_count() == 0


async def test_digest_flush_requeues_undelivered_chunks_on_send_failure(db: Database):
    settings = GlobalSettings(owner_chat_id=42, digest_mode=True, digest_interval_minutes=15)

    calls = []

    async def failing_send(chat_id, text, **kwargs):
        calls.append(text)
        raise RuntimeError("сеть недоступна")

    notifier = Notifier(failing_send, db, lambda: settings)
    pid = await db.add_profile("A", "https://www.avito.ru/1", None, [])
    profile = make_profile(id=pid)
    card = ListingCard(listing_id="1", title="iPhone", price=1000, url="https://x")
    await notifier.enqueue(build_new_listing_event(profile, card))

    await notifier.flush_digest_if_due()

    assert len(calls) == 1  # попытка отправки была
    assert notifier.pending_digest_count() == 1  # но недоставленное вернулось в очередь


async def test_notify_profile_added_summary_text(db: Database):
    settings = GlobalSettings(owner_chat_id=42)
    recorder = Recorder()
    notifier = Notifier(recorder, db, lambda: settings)
    profile = make_profile(name="iPhone 13 128GB")

    await notifier.notify_profile_added_summary(profile, 7)

    assert len(recorder.sent) == 1
    text = recorder.sent[0][1]
    assert "iPhone 13 128GB" in text
    assert "7" in text
