"""SQLite слой хранения (aiosqlite). Хранит профили, seen_listings, настройки,
состояние капчи, журнал уведомлений и событий (п.5, п.11, п.13 ТЗ)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from avito_watcher.models import CaptchaState, GlobalSettings, Profile, SeenListing

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    search_url TEXT NOT NULL,
    price_ceiling INTEGER,
    stop_words TEXT NOT NULL DEFAULT '[]',
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS seen_listings (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    listing_id TEXT NOT NULL,
    last_price INTEGER,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, listing_id)
);

CREATE TABLE IF NOT EXISTS global_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_chat_id INTEGER NOT NULL,
    request_budget_seconds INTEGER NOT NULL DEFAULT 75,
    digest_mode INTEGER NOT NULL DEFAULT 0,
    digest_interval_minutes INTEGER NOT NULL DEFAULT 15,
    quiet_hours_enabled INTEGER NOT NULL DEFAULT 0,
    quiet_hours_start TEXT NOT NULL DEFAULT '00:00',
    quiet_hours_end TEXT NOT NULL DEFAULT '06:00',
    quiet_hours_multiplier REAL NOT NULL DEFAULT 3.0,
    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    paused_all INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS captcha_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER NOT NULL DEFAULT 0,
    resume_at TEXT,
    strikes INTEGER NOT NULL DEFAULT 0,
    last_captcha_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    price INTEGER,
    old_price INTEGER,
    url TEXT NOT NULL,
    location TEXT,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    profile_id INTEGER,
    detail TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at TEXT NOT NULL,
    layout_broken INTEGER NOT NULL DEFAULT 0
);
"""


def _fmt(dt: datetime) -> str:
    # Пробел вместо 'T' — тот же формат, что выдаёт SQLite datetime()/date(),
    # иначе строковое сравнение вида `ts >= datetime('now', '-1 day')`
    # сравнивает 'T' (0x54) с ' ' (0x20) раньше, чем реальное время суток,
    # и даёт неверный результат независимо от фактического часа.
    return dt.isoformat(sep=" ", timespec="seconds")


def _now() -> str:
    return _fmt(datetime.utcnow())


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _row_to_profile(row: aiosqlite.Row) -> Profile:
    return Profile(
        id=row["id"],
        name=row["name"],
        search_url=row["search_url"],
        price_ceiling=row["price_ceiling"],
        stop_words=json.loads(row["stop_words"]) if row["stop_words"] else [],
        paused=bool(row["paused"]),
        created_at=_parse_dt(row["created_at"]),
        last_checked_at=_parse_dt(row["last_checked_at"]),
    )


def _row_to_settings(row: aiosqlite.Row) -> GlobalSettings:
    return GlobalSettings(
        owner_chat_id=row["owner_chat_id"],
        request_budget_seconds=row["request_budget_seconds"],
        digest_mode=bool(row["digest_mode"]),
        digest_interval_minutes=row["digest_interval_minutes"],
        quiet_hours_enabled=bool(row["quiet_hours_enabled"]),
        quiet_hours_start=row["quiet_hours_start"],
        quiet_hours_end=row["quiet_hours_end"],
        quiet_hours_multiplier=row["quiet_hours_multiplier"],
        timezone=row["timezone"],
        paused_all=bool(row["paused_all"]),
    )


def _row_to_captcha(row: aiosqlite.Row) -> CaptchaState:
    return CaptchaState(
        active=bool(row["active"]),
        resume_at=_parse_dt(row["resume_at"]),
        strikes=row["strikes"],
        last_captcha_at=_parse_dt(row["last_captcha_at"]),
    )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database не подключена, вызовите connect()"
        return self._conn

    # ---------- bootstrap ----------

    async def ensure_global_settings(self, owner_chat_id: int, timezone: str = "Europe/Moscow") -> None:
        cur = await self.conn.execute("SELECT id FROM global_settings WHERE id = 1")
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO global_settings (id, owner_chat_id, timezone) VALUES (1, ?, ?)",
                (owner_chat_id, timezone),
            )
        await self.conn.execute(
            "INSERT OR IGNORE INTO captcha_state (id, active, strikes) VALUES (1, 0, 0)"
        )
        cur = await self.conn.execute("SELECT id FROM process_state WHERE id = 1")
        row = await cur.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO process_state (id, started_at, layout_broken) VALUES (1, ?, 0)",
                (_now(),),
            )
        else:
            await self.conn.execute(
                "UPDATE process_state SET started_at = ? WHERE id = 1", (_now(),)
            )
        await self.conn.commit()

    # ---------- profiles ----------

    async def add_profile(
        self,
        name: str,
        search_url: str,
        price_ceiling: Optional[int],
        stop_words: list[str],
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO profiles (name, search_url, price_ceiling, stop_words, paused, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (name, search_url, price_ceiling, json.dumps(stop_words, ensure_ascii=False), _now()),
        )
        await self.conn.commit()
        await self.log_event("owner_action", None, f"profile_added:{name}")
        return cur.lastrowid

    async def get_profile(self, profile_id: int) -> Optional[Profile]:
        cur = await self.conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        row = await cur.fetchone()
        return _row_to_profile(row) if row else None

    async def list_profiles(self) -> list[Profile]:
        cur = await self.conn.execute("SELECT * FROM profiles ORDER BY id")
        rows = await cur.fetchall()
        return [_row_to_profile(r) for r in rows]

    async def list_active_profile_ids(self) -> list[int]:
        cur = await self.conn.execute("SELECT id FROM profiles WHERE paused = 0 ORDER BY id")
        rows = await cur.fetchall()
        return [r["id"] for r in rows]

    async def count_active_profiles(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE paused = 0")
        row = await cur.fetchone()
        return row["c"]

    async def update_profile(
        self,
        profile_id: int,
        *,
        name: Optional[str] = None,
        search_url: Optional[str] = None,
        price_ceiling: Optional[int] = ...,  # type: ignore[assignment]
        stop_words: Optional[list[str]] = None,
    ) -> None:
        fields = []
        params: list = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if search_url is not None:
            fields.append("search_url = ?")
            params.append(search_url)
        if price_ceiling is not ...:
            fields.append("price_ceiling = ?")
            params.append(price_ceiling)
        if stop_words is not None:
            fields.append("stop_words = ?")
            params.append(json.dumps(stop_words, ensure_ascii=False))
        if not fields:
            return
        params.append(profile_id)
        await self.conn.execute(f"UPDATE profiles SET {', '.join(fields)} WHERE id = ?", params)
        await self.conn.commit()

    async def set_profile_paused(self, profile_id: int, paused: bool) -> None:
        await self.conn.execute(
            "UPDATE profiles SET paused = ? WHERE id = ?", (int(paused), profile_id)
        )
        await self.conn.commit()

    async def delete_profile(self, profile_id: int) -> None:
        await self.conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await self.conn.commit()

    async def touch_profile_checked(self, profile_id: int, when: Optional[datetime] = None) -> None:
        await self.conn.execute(
            "UPDATE profiles SET last_checked_at = ? WHERE id = ?",
            (_fmt(when or datetime.utcnow()), profile_id),
        )
        await self.conn.commit()

    # ---------- seen listings ----------

    async def get_seen_listings(self, profile_id: int) -> dict[str, SeenListing]:
        cur = await self.conn.execute(
            "SELECT * FROM seen_listings WHERE profile_id = ?", (profile_id,)
        )
        rows = await cur.fetchall()
        return {
            r["listing_id"]: SeenListing(
                listing_id=r["listing_id"],
                last_price=r["last_price"],
                first_seen_at=_parse_dt(r["first_seen_at"]),
            )
            for r in rows
        }

    async def upsert_seen_listing(
        self,
        profile_id: int,
        listing_id: str,
        price: Optional[int],
        first_seen_at: Optional[datetime] = None,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO seen_listings (profile_id, listing_id, last_price, first_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(profile_id, listing_id)
               DO UPDATE SET last_price = excluded.last_price""",
            (profile_id, listing_id, price, _fmt(first_seen_at or datetime.utcnow())),
        )
        await self.conn.commit()

    # ---------- global settings ----------

    async def get_settings(self) -> GlobalSettings:
        cur = await self.conn.execute("SELECT * FROM global_settings WHERE id = 1")
        row = await cur.fetchone()
        return _row_to_settings(row)

    async def update_settings(self, **kwargs) -> None:
        column_map = {
            "request_budget_seconds": kwargs.get("request_budget_seconds"),
            "digest_mode": (
                int(kwargs["digest_mode"]) if "digest_mode" in kwargs else None
            ),
            "digest_interval_minutes": kwargs.get("digest_interval_minutes"),
            "quiet_hours_enabled": (
                int(kwargs["quiet_hours_enabled"]) if "quiet_hours_enabled" in kwargs else None
            ),
            "quiet_hours_start": kwargs.get("quiet_hours_start"),
            "quiet_hours_end": kwargs.get("quiet_hours_end"),
            "quiet_hours_multiplier": kwargs.get("quiet_hours_multiplier"),
            "timezone": kwargs.get("timezone"),
            "paused_all": (int(kwargs["paused_all"]) if "paused_all" in kwargs else None),
        }
        fields = []
        params: list = []
        for col, val in column_map.items():
            if col in kwargs:
                fields.append(f"{col} = ?")
                params.append(val)
        if not fields:
            return
        await self.conn.execute(f"UPDATE global_settings SET {', '.join(fields)} WHERE id = 1", params)
        await self.conn.commit()

    # ---------- captcha state ----------

    async def get_captcha_state(self) -> CaptchaState:
        cur = await self.conn.execute("SELECT * FROM captcha_state WHERE id = 1")
        row = await cur.fetchone()
        return _row_to_captcha(row)

    async def set_captcha_state(
        self,
        *,
        active: bool,
        resume_at: Optional[datetime] = None,
        strikes: Optional[int] = None,
        last_captcha_at: Optional[datetime] = None,
    ) -> None:
        # resume_at выставляется безусловно (включая явный сброс в None при
        # возобновлении) — вызывающий код всегда передаёт его осознанно.
        fields = ["active = ?", "resume_at = ?"]
        params: list = [int(active), _fmt(resume_at) if resume_at else None]
        if strikes is not None:
            fields.append("strikes = ?")
            params.append(strikes)
        if last_captcha_at is not None:
            fields.append("last_captcha_at = ?")
            params.append(_fmt(last_captcha_at))
        await self.conn.execute(f"UPDATE captcha_state SET {', '.join(fields)} WHERE id = 1", params)
        await self.conn.commit()

    # ---------- notifications log ----------

    async def log_notification(
        self,
        profile_id: int,
        kind: str,
        title: str,
        price: Optional[int],
        old_price: Optional[int],
        url: str,
        location: str,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO notifications_log
               (profile_id, kind, title, price, old_price, url, location, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, kind, title, price, old_price, url, location, _now()),
        )
        await self.conn.commit()

    async def get_recent_notifications(self, profile_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM notifications_log WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
            (profile_id, limit),
        )
        return list(await cur.fetchall())

    async def count_notifications_today(self, profile_id: int) -> int:
        cur = await self.conn.execute(
            """SELECT COUNT(*) AS c FROM notifications_log
               WHERE profile_id = ? AND date(sent_at) = date('now')""",
            (profile_id,),
        )
        row = await cur.fetchone()
        return row["c"]

    # ---------- events log ----------

    async def log_event(self, event_type: str, profile_id: Optional[int], detail: str) -> None:
        await self.conn.execute(
            "INSERT INTO events_log (event_type, profile_id, detail, ts) VALUES (?, ?, ?, ?)",
            (event_type, profile_id, detail, _now()),
        )
        await self.conn.commit()

    async def count_events_last_24h(self, event_types: tuple[str, ...]) -> int:
        placeholders = ",".join("?" for _ in event_types)
        cur = await self.conn.execute(
            f"""SELECT COUNT(*) AS c FROM events_log
                WHERE event_type IN ({placeholders})
                AND ts >= datetime('now', '-1 day')""",
            event_types,
        )
        row = await cur.fetchone()
        return row["c"]

    # ---------- process state (layout / uptime) ----------

    async def get_started_at(self) -> datetime:
        cur = await self.conn.execute("SELECT started_at FROM process_state WHERE id = 1")
        row = await cur.fetchone()
        return _parse_dt(row["started_at"])

    async def get_layout_broken(self) -> bool:
        cur = await self.conn.execute("SELECT layout_broken FROM process_state WHERE id = 1")
        row = await cur.fetchone()
        return bool(row["layout_broken"])

    async def set_layout_broken(self, broken: bool) -> None:
        await self.conn.execute(
            "UPDATE process_state SET layout_broken = ? WHERE id = 1", (int(broken),)
        )
        await self.conn.commit()
