"""Проверка логики диффа объявлений в WatcherEngine.check_profile без
реального Playwright — self.page подменяется лёгким фейком."""

from pathlib import Path

import pytest

from avito_watcher.db import Database
from avito_watcher.models import GlobalSettings
from avito_watcher.notifications import Notifier
from avito_watcher.scheduler import Scheduler
from avito_watcher.watcher import WatcherEngine

CARD_HTML = """
<html><body>
<div data-marker="item" data-item-id="1111111111">
  <a data-marker="item-title" href="/moskva/x_1111111111">iPhone 13</a>
  <meta itemprop="price" content="{price}">
  <span data-marker="item-address">Москва</span>
</div>
</body></html>
"""


class FakeMouse:
    async def wheel(self, dx, dy):
        pass


class FakePage:
    def __init__(self, html_sequence):
        self._html_sequence = list(html_sequence)
        self.mouse = FakeMouse()
        self.url = "https://www.avito.ru/moskva/x"

    async def goto(self, url, wait_until=None, timeout=None):
        pass

    async def content(self):
        return self._html_sequence.pop(0)


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text, **kwargs):
        self.sent.append(text)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    await database.ensure_global_settings(owner_chat_id=42)
    yield database
    await database.close()


async def make_watcher(db: Database, html_sequence: list[str]) -> tuple[WatcherEngine, Recorder]:
    settings = GlobalSettings(owner_chat_id=42)
    recorder = Recorder()
    notifier = Notifier(recorder, db, lambda: settings)
    scheduler = Scheduler(db, lambda: settings)
    watcher = WatcherEngine(db, scheduler, notifier, lambda: settings, Path("/tmp/bp"), Path("/tmp/logs"))
    watcher.page = FakePage(html_sequence)
    return watcher, recorder


async def test_first_pass_stores_baseline_without_notification(db: Database):
    pid = await db.add_profile("iPhone", "https://www.avito.ru/search?x=1", None, [])
    watcher, recorder = await make_watcher(db, [CARD_HTML.format(price=35000)])

    await watcher.check_profile(pid)

    assert len(recorder.sent) == 1
    assert "Добавлен товар" in recorder.sent[0]
    seen = await db.get_seen_listings(pid)
    assert seen["1111111111"].last_price == 35000


async def test_price_increase_then_decrease_below_original_is_detected(db: Database):
    """Регрессия: раньше рост цены не сохранялся, поэтому последующее
    снижение сравнивалось со старой (заниженной) отметкой и терялось,
    если новая цена не опускалась ниже самой первой сохранённой."""
    pid = await db.add_profile("iPhone", "https://www.avito.ru/search?x=1", None, [])
    watcher, recorder = await make_watcher(
        db, [CARD_HTML.format(price=35000), CARD_HTML.format(price=40000), CARD_HTML.format(price=38000)]
    )

    await watcher.check_profile(pid)  # baseline: 35000, без уведомления
    await watcher.check_profile(pid)  # рост до 40000 — должен сохраниться, без уведомления
    seen = await db.get_seen_listings(pid)
    assert seen["1111111111"].last_price == 40000

    recorder.sent.clear()
    await watcher.check_profile(pid)  # падение до 38000 — ниже 40000, хоть и выше исходных 35000
    assert len(recorder.sent) == 1
    assert "38 000" in recorder.sent[0]
    assert "40 000" in recorder.sent[0]


async def test_price_increase_alone_sends_no_notification(db: Database):
    pid = await db.add_profile("iPhone", "https://www.avito.ru/search?x=1", None, [])
    watcher, recorder = await make_watcher(
        db, [CARD_HTML.format(price=35000), CARD_HTML.format(price=40000)]
    )

    await watcher.check_profile(pid)
    recorder.sent.clear()
    await watcher.check_profile(pid)

    assert recorder.sent == []
