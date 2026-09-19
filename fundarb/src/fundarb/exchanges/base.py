"""The sole point of contact with an exchange. Nothing outside `exchanges/`
is allowed to import ccxt directly — that keeps ccxt swappable for a native
SDK later without touching strategy, risk, or execution code.

Read methods are usable from Phase 1 with no API key. Trading methods are
real from Phase 4 onward; until an adapter is wired with keys, calling them
raises `ExchangeAdapterError` rather than silently no-opping, so a strategy
accidentally run against an unconfigured adapter fails loudly instead of
"succeeding" with no orders placed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from fundarb.core.models import (
    Balances,
    Candle,
    FundingRate,
    Instrument,
    Order,
    OrderAck,
    OrderIntent,
    Position,
    Quote,
)
from fundarb.core.types import Market, Venue


class ExchangeAdapter(ABC):
    venue: Venue

    # ---- read (Phase 1) -------------------------------------------------

    @abstractmethod
    async def fetch_funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingRate]: ...

    @abstractmethod
    async def fetch_klines(
        self, symbol: str, market: Market, interval: str, start: datetime, end: datetime
    ) -> list[Candle]: ...

    @abstractmethod
    async def fetch_instruments(self) -> list[Instrument]: ...

    @abstractmethod
    async def get_quote(self, symbol: str, market: Market) -> Quote: ...

    # ---- trading (Phase 3+, stubs raise until keys are wired) -----------

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> OrderAck: ...

    @abstractmethod
    async def cancel_order(self, venue_order_id: str, symbol: str, market: Market) -> None: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def get_balances(self) -> Balances: ...

    @abstractmethod
    async def wait_for_fill(
        self, venue_order_id: str, symbol: str, market: Market, timeout_sec: float
    ) -> OrderAck:
        """Poll/stream until the order reaches a terminal state or the
        timeout elapses. Raises OrderTimeoutError on timeout — callers
        (execution/executor.py) decide what "timeout" means for a leg.
        """

    @abstractmethod
    async def close(self) -> None: ...
