"""Backfills and refreshes stored history. Idempotency is guaranteed at the
storage layer (dedup on primary key), so this module can always be re-run
for an overlapping range without creating duplicates; on restart it resumes
from the last stored record instead of re-downloading everything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from fundarb.collect.storage import ParquetStorage
from fundarb.core.models import Instrument
from fundarb.core.types import Market
from fundarb.exchanges.base import ExchangeAdapter

log = structlog.get_logger(__name__)

_KLINE_INTERVAL = "1h"


class HistoryCollector:
    def __init__(self, adapter: ExchangeAdapter, storage: ParquetStorage) -> None:
        self.adapter = adapter
        self.storage = storage

    async def backfill_symbol(
        self, symbol: str, start: datetime, end: datetime | None = None
    ) -> None:
        end = end or datetime.now(timezone.utc)
        resume_from = self._resume_point(symbol, start)
        if resume_from >= end:
            log.info("funding history up to date", venue=self.adapter.venue, symbol=symbol)
        else:
            rates = await self.adapter.fetch_funding_history(symbol, resume_from, end)
            written = self.storage.write_funding_rates(rates)
            log.info(
                "funding history collected",
                venue=self.adapter.venue,
                symbol=symbol,
                fetched=len(rates),
                written=written,
            )

        for market in (Market.SPOT, Market.PERP):
            candle_resume = self._resume_point_candles(symbol, market, start)
            if candle_resume >= end:
                continue
            candles = await self.adapter.fetch_klines(symbol, market, _KLINE_INTERVAL, candle_resume, end)
            written = self.storage.write_candles(candles)
            log.info(
                "candles collected",
                venue=self.adapter.venue,
                symbol=symbol,
                market=market,
                fetched=len(candles),
                written=written,
            )

    async def backfill_universe(
        self, instruments: list[Instrument], start: datetime, end: datetime | None = None
    ) -> None:
        for instrument in instruments:
            await self.backfill_symbol(instrument.symbol, start, end)

    def _resume_point(self, symbol: str, fallback_start: datetime) -> datetime:
        existing = self.storage.read_funding_rates(self.adapter.venue, symbol)
        if existing.height == 0:
            return fallback_start
        last_ts = existing["funding_time"].max()
        return last_ts.replace(tzinfo=timezone.utc) + timedelta(milliseconds=1)

    def _resume_point_candles(
        self, symbol: str, market: Market, fallback_start: datetime
    ) -> datetime:
        existing = self.storage.read_candles(self.adapter.venue, symbol, market)
        if existing.height == 0:
            return fallback_start
        last_ts = existing["open_time"].max()
        return last_ts.replace(tzinfo=timezone.utc) + timedelta(milliseconds=1)
