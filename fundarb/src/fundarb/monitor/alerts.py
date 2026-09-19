"""Telegram alerting. Disabled by default (no bot token) — the client then
just logs, so monitor.py callers never need to branch on whether alerting
is configured.
"""

from __future__ import annotations

import httpx
import structlog

from fundarb.config import Secrets

log = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerter:
    def __init__(self, secrets: Secrets, *, enabled: bool) -> None:
        self.enabled = enabled and bool(secrets.telegram_bot_token) and bool(secrets.telegram_chat_id)
        self._token = secrets.telegram_bot_token
        self._chat_id = secrets.telegram_chat_id

    async def send(self, text: str) -> None:
        log.warning("alert", text=text)
        if not self.enabled:
            return
        url = _TELEGRAM_API.format(token=self._token)
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.post(url, json={"chat_id": self._chat_id, "text": text})
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("failed to send telegram alert", error=str(exc))

    # ---- the alert events the spec calls out explicitly ------------------

    async def rate_reversal(self, symbol: str, rate_pct: float) -> None:
        await self.send(f"⚠️ {symbol}: funding rate turned negative ({rate_pct:.4f}% per period)")

    async def delta_out_of_tolerance(self, symbol: str, deviation_pct: float, rebalanced: bool) -> None:
        action = "auto-rebalanced" if rebalanced else "ALERT ONLY, not rebalanced"
        await self.send(f"⚠️ {symbol}: delta deviation {deviation_pct:.2f}% — {action}")

    async def margin_low(self, symbol: str, margin_ratio: float) -> None:
        await self.send(f"🚨 {symbol}: margin ratio {margin_ratio:.2%} below alert threshold")

    async def connection_lost(self, venue: str, seconds: float) -> None:
        await self.send(f"🚨 {venue}: connection lost for {seconds:.0f}s")

    async def kill_switch(self, reason: str) -> None:
        await self.send(f"🛑 KILL SWITCH TRIGGERED: {reason}")
