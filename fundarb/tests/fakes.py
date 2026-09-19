"""A minimal fake ExchangeAdapter for execution tests — no network, no ccxt.
Only the trading methods the executor actually calls have real behavior;
everything else raises if accidentally exercised, so a test that reaches
them fails loudly instead of silently doing nothing.
"""

from __future__ import annotations

from decimal import Decimal

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
from fundarb.core.types import Market, OrderStatus, Venue
from fundarb.exchanges.base import ExchangeAdapter


class FakeAdapter(ExchangeAdapter):
    venue = Venue.BINANCE

    def __init__(self, fill_responses: list[OrderAck | Exception] | None = None) -> None:
        self._fill_responses = list(fill_responses or [])
        self.placed_intents: list[OrderIntent] = []

    async def place_order(self, intent: OrderIntent) -> OrderAck:
        self.placed_intents.append(intent)
        return OrderAck(
            venue=self.venue,
            client_order_id=intent.client_order_id,
            venue_order_id=f"v-{len(self.placed_intents)}",
            status=OrderStatus.NEW,
            filled_quantity=Decimal(0),
        )

    async def wait_for_fill(
        self, venue_order_id: str, symbol: str, market: Market, timeout_sec: float
    ) -> OrderAck:
        resp = self._fill_responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def fetch_funding_history(self, symbol, start, end) -> list[FundingRate]:
        raise NotImplementedError

    async def fetch_klines(self, symbol, market, interval, start, end) -> list[Candle]:
        raise NotImplementedError

    async def fetch_instruments(self) -> list[Instrument]:
        raise NotImplementedError

    async def get_quote(self, symbol: str, market: Market) -> Quote:
        raise NotImplementedError

    async def cancel_order(self, venue_order_id: str, symbol: str, market: Market) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    async def get_open_orders(self) -> list[Order]:
        raise NotImplementedError

    async def get_balances(self) -> Balances:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def filled_ack(venue: Venue, client_order_id: str, quantity: str) -> OrderAck:
    return OrderAck(
        venue=venue,
        client_order_id=client_order_id,
        venue_order_id="v-fill",
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(quantity),
        avg_fill_price=Decimal("50000"),
    )
