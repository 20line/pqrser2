"""Fakes for execution/live-runner tests — no network, no ccxt.

FakeAdapter starts with every trading/read method raising NotImplementedError
so a test that reaches an untouched capability fails loudly. Configure only
what a given test actually exercises via the constructor or by setting
attributes directly before calling into it.
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

    def __init__(
        self,
        fill_responses: list[OrderAck | Exception] | None = None,
        *,
        quotes: dict[Market, Quote] | None = None,
        positions: list[Position] | None = None,
        open_orders: list[Order] | None = None,
        funding_history: list[FundingRate] | None = None,
        klines: list[Candle] | None = None,
        balances: Balances | None = None,
        perp_margin_balance: Decimal = Decimal("100000"),
        raise_on_quote: Exception | None = None,
        raise_on_set_leverage: Exception | None = None,
        raise_on_margin_balance: Exception | None = None,
    ) -> None:
        self._fill_responses = list(fill_responses or [])
        self.placed_intents: list[OrderIntent] = []
        self.quotes = dict(quotes or {})
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.funding_history = list(funding_history or [])
        self.klines = list(klines or [])
        self.balances = balances or Balances(venue=self.venue, total={}, free={}, used={})
        self.perp_margin_balance = perp_margin_balance
        self.raise_on_quote = raise_on_quote
        self.raise_on_set_leverage = raise_on_set_leverage
        self.raise_on_margin_balance = raise_on_margin_balance
        self.set_leverage_calls: list[tuple[str, Decimal]] = []
        self.closed = False

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
        return [r for r in self.funding_history if start <= r.funding_time <= end]

    async def fetch_klines(self, symbol, market, interval, start, end) -> list[Candle]:
        return [c for c in self.klines if c.market is market and start <= c.open_time <= end]

    async def fetch_instruments(self) -> list[Instrument]:
        raise NotImplementedError

    async def get_quote(self, symbol: str, market: Market) -> Quote:
        if self.raise_on_quote:
            raise self.raise_on_quote
        return self.quotes[market]

    async def cancel_order(self, venue_order_id: str, symbol: str, market: Market) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return self.positions

    async def get_open_orders(self) -> list[Order]:
        return self.open_orders

    async def get_balances(self) -> Balances:
        return self.balances

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        if self.raise_on_set_leverage:
            raise self.raise_on_set_leverage
        self.set_leverage_calls.append((symbol, leverage))

    async def get_perp_margin_balance(self, asset: str) -> Decimal:
        if self.raise_on_margin_balance:
            raise self.raise_on_margin_balance
        return self.perp_margin_balance

    async def close(self) -> None:
        self.closed = True


def filled_ack(venue: Venue, client_order_id: str, quantity: str, price: str = "50000") -> OrderAck:
    return OrderAck(
        venue=venue,
        client_order_id=client_order_id,
        venue_order_id="v-fill",
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(quantity),
        avg_fill_price=Decimal(price),
    )


class RecordingAlerter:
    """Stands in for monitor.alerts.TelegramAlerter — records every call
    instead of logging/sending, so tests can assert on what fired.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def send(self, text: str) -> None:
        self.calls.append(("send", (text,)))

    async def rate_reversal(self, symbol: str, rate_pct: float) -> None:
        self.calls.append(("rate_reversal", (symbol, rate_pct)))

    async def delta_out_of_tolerance(self, symbol: str, deviation_pct: float, rebalanced: bool) -> None:
        self.calls.append(("delta_out_of_tolerance", (symbol, deviation_pct, rebalanced)))

    async def margin_low(self, symbol: str, margin_ratio: float) -> None:
        self.calls.append(("margin_low", (symbol, margin_ratio)))

    async def connection_lost(self, venue: str, seconds: float) -> None:
        self.calls.append(("connection_lost", (venue, seconds)))

    async def kill_switch(self, reason: str) -> None:
        self.calls.append(("kill_switch", (reason,)))
