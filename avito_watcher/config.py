"""Загрузка статической конфигурации (токен бота, owner_chat_id, пути) из .env.

Изменяемые в рантайме настройки (digest, тихие часы, бюджет запросов) живут
в SQLite (см. db.py) и редактируются через Telegram-меню «⚙️ Настройки».
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    owner_chat_id: int
    db_path: Path
    log_dir: Path
    browser_profile_dir: Path
    timezone: str = "Europe/Moscow"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5


class ConfigError(RuntimeError):
    pass


def load_config(env_path: Path | None = None) -> AppConfig:
    load_dotenv(dotenv_path=env_path or (BASE_DIR / ".env"))

    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN не задан в .env")

    owner_raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    if not owner_raw:
        raise ConfigError("OWNER_CHAT_ID не задан в .env")
    try:
        owner_chat_id = int(owner_raw)
    except ValueError as exc:
        raise ConfigError("OWNER_CHAT_ID должен быть целым числом") from exc

    db_path = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "avito_watcher.db")))
    log_dir = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))
    browser_profile_dir = Path(
        os.environ.get("BROWSER_PROFILE_DIR", str(BASE_DIR / "data" / "browser_profile"))
    )
    timezone = os.environ.get("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"

    db_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    browser_profile_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        bot_token=token,
        owner_chat_id=owner_chat_id,
        db_path=db_path,
        log_dir=log_dir,
        browser_profile_dir=browser_profile_dir,
        timezone=timezone,
    )
