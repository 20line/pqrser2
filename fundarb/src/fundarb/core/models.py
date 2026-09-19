"""The three contracts that define the system (FundingRate, ExchangeAdapter's
return types, OrderIntent) plus their supporting models. All monetary and
rate fields use Decimal — funding accrues hundreds of times over a position's
life, and float rounding error compounds into a real PnL discrepancy.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from fundarb.core.types import (
    IntentReason,
    Market,
    OrderSide,
    OrderStatus,
    OrderType,
    Venue,
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class FundingRate(_Model):
    """One funding accrual record — the unit of stored history."""

    venue: Venue
    symbol: str  # normalized, e.g. "BTC/USDT"
    funding_time: datetime  # UTC
    rate: Decimal  # per-period rate, NOT annualized
    interval_hours: int  # 1, 4 or 8 — fixed at record time, contracts can change it
    mark_price: Decimal


class Candle(_Model):
    venue: Venue
    symbol: str
    market: Market
    interval: str  # exchange-native kline interval, e.g. "1h"
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class Instrument(_Model):
    venue: Venue
    symbol: str
    spot_symbol: str | None = None
    perp_symbol: str | None = None
    funding_interval_hours: int
    price_step: Decimal
    qty_step: Decimal
    min_qty: Decimal
    listed_at: datetime | None = None
    spot_quote_volume_24h_usd: Decimal | None = None
    perp_quote_volume_24h_usd: Decimal | None = None


class Quote(_Model):
    venue: Venue
    symbol: str
    market: Market
    bid: Decimal
    ask: Decimal
    timestamp: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class Position(_Model):
    venue: Venue
    symbol: str
    market: Market
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: Decimal = Decimal(1)
    unrealized_pnl: Decimal = Decimal(0)


class Balances(_Model):
    venue: Venue
    total: dict[str, Decimal]
    free: dict[str, Decimal]
    used: dict[str, Decimal]


class Order(_Model):
    venue: Venue
    symbol: str
    market: Market
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    filled_quantity: Decimal = Decimal(0)
    limit_price: Decimal | None = None
    status: OrderStatus = OrderStatus.NEW
    client_order_id: str
    venue_order_id: str | None = None


class OrderAck(_Model):
    """What the exchange returns right after placing an order."""

    venue: Venue
    client_order_id: str
    venue_order_id: str
    status: OrderStatus
    filled_quantity: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None


class OrderIntent(_Model):
    """An intent, not an order. The strategy produces this; the risk module
    approves or rejects it; only the executor turns an approved intent into
    an exchange call. `reason` is mandatory — without it, reconstructing why
    the system did something after the fact is guesswork.
    """

    venue: Venue
    symbol: str
    market: Market
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    reduce_only: bool = False
    client_order_id: str
    reason: IntentReason
