"""Shared ccxt-backed implementation of ExchangeAdapter. Binance and Bybit
differ only in exchange id, ccxt market-type kwargs, and funding-interval
lookup — everything else (pagination, symbol translation, order lifecycle)
is identical, so it lives here once instead of twice.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import ccxt.pro as ccxtpro

from fundarb.core.errors import ExchangeAdapterError, OrderTimeoutError
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
from fundarb.core.types import Market, OrderSide, OrderStatus, OrderType
from fundarb.exchanges.base import ExchangeAdapter
from fundarb.exchanges.symbols import from_ccxt_symbol, normalize, to_ccxt_symbol

_MS = 1000
_PAGE_LIMIT_DEFAULT = 200  # matches Binance's un-ranged funding history cap

_STATUS_MAP = {
    "open": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def _to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / _MS, tz=timezone.utc)


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * _MS)


class CCXTExchangeAdapter(ExchangeAdapter):
    """Common ccxt.pro plumbing. Subclasses set `ccxt_id`, the perp/spot
    market-type kwargs, and the default funding interval lookup.
    """

    ccxt_id: str
    default_funding_interval_hours = 8

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
        page_limit: int = _PAGE_LIMIT_DEFAULT,
        rate_limit_sleep_sec: float = 0.0,
    ) -> None:
        self._page_limit = page_limit
        self._rate_limit_sleep_sec = rate_limit_sleep_sec
        exchange_cls = getattr(ccxtpro, self.ccxt_id)
        self._spot = exchange_cls(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self._perp = exchange_cls(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
        )
        if testnet:
            self._spot.set_sandbox_mode(True)
            self._perp.set_sandbox_mode(True)
        self._client_id_prefix = f"fundarb-{self.venue.value}"

    def _client(self, market: Market):
        return self._spot if market is Market.SPOT else self._perp

    # ---- read -------------------------------------------------------

    async def fetch_funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingRate]:
        ccxt_symbol = to_ccxt_symbol(symbol, Market.PERP)
        out: list[FundingRate] = []
        since = _to_ms(start)
        end_ms = _to_ms(end)
        interval_hours = await self._funding_interval_hours(symbol)
        try:
            while since < end_ms:
                page = await self._perp.fetch_funding_rate_history(
                    ccxt_symbol, since=since, limit=self._page_limit
                )
                if not page:
                    break
                for row in page:
                    ts = row["timestamp"]
                    if ts is None or ts >= end_ms:
                        continue
                    out.append(
                        FundingRate(
                            venue=self.venue,
                            symbol=symbol,
                            funding_time=_to_dt(ts),
                            rate=_dec(row.get("fundingRate")),
                            interval_hours=interval_hours,
                            mark_price=_dec(row.get("markPrice") or row.get("info", {}).get("markPrice")),
                        )
                    )
                last_ts = page[-1]["timestamp"]
                if last_ts is None or last_ts <= since:
                    break
                since = last_ts + 1
                if self._rate_limit_sleep_sec:
                    await asyncio.sleep(self._rate_limit_sleep_sec)
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: fetch_funding_history failed: {exc}") from exc
        return out

    async def _funding_interval_hours(self, symbol: str) -> int:
        try:
            markets = self._perp.markets or await self._perp.load_markets()
            info = markets.get(to_ccxt_symbol(symbol, Market.PERP), {})
            interval_ms = info.get("info", {}).get("fundingIntervalHours")
            if interval_ms:
                return int(interval_ms)
        except Exception:  # noqa: BLE001 - best effort, fall back below
            pass
        return self.default_funding_interval_hours

    async def fetch_klines(
        self, symbol: str, market: Market, interval: str, start: datetime, end: datetime
    ) -> list[Candle]:
        ccxt_symbol = to_ccxt_symbol(symbol, market)
        client = self._client(market)
        out: list[Candle] = []
        since = _to_ms(start)
        end_ms = _to_ms(end)
        try:
            while since < end_ms:
                page = await client.fetch_ohlcv(
                    ccxt_symbol, timeframe=interval, since=since, limit=self._page_limit
                )
                if not page:
                    break
                for ts, o, h, l, c, v in page:
                    if ts >= end_ms:
                        continue
                    out.append(
                        Candle(
                            venue=self.venue,
                            symbol=symbol,
                            market=market,
                            interval=interval,
                            open_time=_to_dt(ts),
                            open=_dec(o),
                            high=_dec(h),
                            low=_dec(l),
                            close=_dec(c),
                            volume=_dec(v),
                        )
                    )
                last_ts = page[-1][0]
                if last_ts <= since:
                    break
                since = last_ts + 1
                if self._rate_limit_sleep_sec:
                    await asyncio.sleep(self._rate_limit_sleep_sec)
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: fetch_klines failed: {exc}") from exc
        return out

    async def fetch_instruments(self) -> list[Instrument]:
        try:
            spot_markets = await self._spot.load_markets(reload=True)
            perp_markets = await self._perp.load_markets(reload=True)
            spot_tickers = await self._spot.fetch_tickers()
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: fetch_instruments failed: {exc}") from exc

        out: list[Instrument] = []
        for perp_symbol, perp_info in perp_markets.items():
            if not perp_info.get("swap") or not perp_info.get("linear"):
                continue
            base, quote = perp_info["base"], perp_info["quote"]
            symbol = normalize(base, quote)
            spot_symbol = f"{base}/{quote}"
            if spot_symbol not in spot_markets:
                continue
            ticker = spot_tickers.get(spot_symbol, {})
            interval_hours = perp_info.get("info", {}).get(
                "fundingIntervalHours", self.default_funding_interval_hours
            )
            listed_ms = perp_info.get("info", {}).get("onboardDate") or perp_info.get("info", {}).get("launchTime")
            out.append(
                Instrument(
                    venue=self.venue,
                    symbol=symbol,
                    spot_symbol=spot_symbol,
                    perp_symbol=perp_symbol,
                    funding_interval_hours=int(interval_hours or self.default_funding_interval_hours),
                    price_step=_dec(perp_info.get("precision", {}).get("price") or "0.0001"),
                    qty_step=_dec(perp_info.get("precision", {}).get("amount") or "0.0001"),
                    min_qty=_dec((perp_info.get("limits", {}).get("amount") or {}).get("min") or "0"),
                    listed_at=_to_dt(int(listed_ms)) if listed_ms else None,
                    quote_volume_24h_usd=_dec(ticker.get("quoteVolume")) if ticker else None,
                )
            )
        return out

    async def get_quote(self, symbol: str, market: Market) -> Quote:
        client = self._client(market)
        ccxt_symbol = to_ccxt_symbol(symbol, market)
        try:
            ticker = await client.fetch_ticker(ccxt_symbol)
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: get_quote failed: {exc}") from exc
        return Quote(
            venue=self.venue,
            symbol=symbol,
            market=market,
            bid=_dec(ticker.get("bid")),
            ask=_dec(ticker.get("ask")),
            timestamp=_to_dt(ticker["timestamp"]) if ticker.get("timestamp") else datetime.now(timezone.utc),
        )

    # ---- trading ------------------------------------------------------

    def new_client_order_id(self, seed: str) -> str:
        """Deterministic per logical action: resending the same (reason,
        symbol, leg) after a disconnect reuses the same id instead of
        creating a duplicate order.
        """
        return f"{self._client_id_prefix}-{seed}"[:36]

    async def place_order(self, intent: OrderIntent) -> OrderAck:
        client = self._client(intent.market)
        ccxt_symbol = to_ccxt_symbol(intent.symbol, intent.market)
        params: dict[str, Any] = {"clientOrderId": intent.client_order_id}
        if intent.reduce_only and intent.market is Market.PERP:
            params["reduceOnly"] = True
        try:
            order = await client.create_order(
                ccxt_symbol,
                intent.order_type.value,
                intent.side.value,
                float(intent.quantity),
                float(intent.limit_price) if intent.limit_price is not None else None,
                params,
            )
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: place_order failed: {exc}") from exc
        return self._order_to_ack(order, intent.client_order_id)

    def _order_to_ack(self, order: dict[str, Any], client_order_id: str) -> OrderAck:
        status = _STATUS_MAP.get(order.get("status") or "open", OrderStatus.NEW)
        filled = _dec(order.get("filled"))
        return OrderAck(
            venue=self.venue,
            client_order_id=order.get("clientOrderId") or client_order_id,
            venue_order_id=str(order.get("id")),
            status=status,
            filled_quantity=filled,
            avg_fill_price=_dec(order.get("average")) if order.get("average") else None,
        )

    async def cancel_order(self, venue_order_id: str, symbol: str, market: Market) -> None:
        client = self._client(market)
        ccxt_symbol = to_ccxt_symbol(symbol, market)
        try:
            await client.cancel_order(venue_order_id, ccxt_symbol)
        except ccxtpro.OrderNotFound:
            return
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: cancel_order failed: {exc}") from exc

    async def get_positions(self) -> list[Position]:
        try:
            raw = await self._perp.fetch_positions()
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: get_positions failed: {exc}") from exc
        out: list[Position] = []
        for p in raw:
            qty = _dec(p.get("contracts"))
            if qty == 0:
                continue
            out.append(
                Position(
                    venue=self.venue,
                    symbol=from_ccxt_symbol(p["symbol"]),
                    market=Market.PERP,
                    side=OrderSide.BUY if p.get("side") == "long" else OrderSide.SELL,
                    quantity=qty,
                    entry_price=_dec(p.get("entryPrice")),
                    mark_price=_dec(p.get("markPrice")),
                    leverage=_dec(p.get("leverage") or "1"),
                    unrealized_pnl=_dec(p.get("unrealizedPnl")),
                )
            )
        return out

    async def get_open_orders(self) -> list[Order]:
        out: list[Order] = []
        for market, client in ((Market.SPOT, self._spot), (Market.PERP, self._perp)):
            try:
                raw = await client.fetch_open_orders()
            except ccxtpro.BaseError as exc:
                raise ExchangeAdapterError(f"{self.venue}: get_open_orders failed: {exc}") from exc
            for o in raw:
                out.append(
                    Order(
                        venue=self.venue,
                        symbol=from_ccxt_symbol(o["symbol"]),
                        market=market,
                        side=OrderSide(o["side"]),
                        order_type=OrderType.LIMIT if o.get("type") == "limit" else OrderType.MARKET,
                        quantity=_dec(o.get("amount")),
                        filled_quantity=_dec(o.get("filled")),
                        limit_price=_dec(o.get("price")) if o.get("price") else None,
                        status=_STATUS_MAP.get(o.get("status") or "open", OrderStatus.NEW),
                        client_order_id=o.get("clientOrderId") or "",
                        venue_order_id=str(o.get("id")),
                    )
                )
        return out

    async def get_balances(self) -> Balances:
        try:
            spot_bal = await self._spot.fetch_balance()
            perp_bal = await self._perp.fetch_balance()
        except ccxtpro.BaseError as exc:
            raise ExchangeAdapterError(f"{self.venue}: get_balances failed: {exc}") from exc
        total: dict[str, Decimal] = {}
        free: dict[str, Decimal] = {}
        used: dict[str, Decimal] = {}
        for bal in (spot_bal, perp_bal):
            for asset, amount in (bal.get("total") or {}).items():
                total[asset] = total.get(asset, Decimal(0)) + _dec(amount)
            for asset, amount in (bal.get("free") or {}).items():
                free[asset] = free.get(asset, Decimal(0)) + _dec(amount)
            for asset, amount in (bal.get("used") or {}).items():
                used[asset] = used.get(asset, Decimal(0)) + _dec(amount)
        return Balances(venue=self.venue, total=total, free=free, used=used)

    async def wait_for_fill(
        self, venue_order_id: str, symbol: str, market: Market, timeout_sec: float
    ) -> OrderAck:
        client = self._client(market)
        ccxt_symbol = to_ccxt_symbol(symbol, market)
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                order = await client.fetch_order(venue_order_id, ccxt_symbol)
            except ccxtpro.BaseError as exc:
                raise ExchangeAdapterError(f"{self.venue}: wait_for_fill failed: {exc}") from exc
            status = _STATUS_MAP.get(order.get("status") or "open", OrderStatus.NEW)
            if status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                return self._order_to_ack(order, order.get("clientOrderId") or "")
            await asyncio.sleep(0.5)
        raise OrderTimeoutError(
            f"{self.venue}: order {venue_order_id} ({symbol}/{market}) did not fill within {timeout_sec}s"
        )

    async def close(self) -> None:
        await self._spot.close()
        await self._perp.close()
