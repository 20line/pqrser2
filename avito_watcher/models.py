"""Датаклассы предметной области: Profile, GlobalSettings, события и т.д."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SeenListing:
    listing_id: str
    last_price: Optional[int]
    first_seen_at: datetime


@dataclass
class Profile:
    id: int
    name: str
    search_url: str
    price_ceiling: Optional[int]
    stop_words: list[str]
    paused: bool
    created_at: datetime
    last_checked_at: Optional[datetime]

    @property
    def is_first_run(self) -> bool:
        return self.last_checked_at is None


@dataclass
class GlobalSettings:
    owner_chat_id: int
    request_budget_seconds: int = 75
    digest_mode: bool = False
    digest_interval_minutes: int = 15
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "06:00"
    quiet_hours_multiplier: float = 3.0
    timezone: str = "Europe/Moscow"
    paused_all: bool = False


@dataclass
class CaptchaState:
    active: bool = False
    resume_at: Optional[datetime] = None
    strikes: int = 0
    last_captcha_at: Optional[datetime] = None


@dataclass
class ListingCard:
    listing_id: str
    title: str
    price: Optional[int]
    url: str
    location: str = ""


@dataclass
class NotificationEvent:
    kind: str  # "new" | "price_drop"
    profile_id: int
    profile_name: str
    title: str
    url: str
    price: Optional[int]
    old_price: Optional[int] = None
    location: str = ""
    good_price: bool = False
    sent_at: Optional[datetime] = None
