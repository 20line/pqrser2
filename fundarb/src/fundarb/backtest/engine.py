"""Event-driven backtest: replays stored funding events in order, tracks a
single open/flat position per symbol, and models the costs that make the
difference between "the script ran" and "the number is real": both legs'
fees, slippage as a function of size versus recent volume, funding accrued
on the actual historical schedule, and basis drift between entry and exit.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import polars as pl
import structlog

from fundarb.backtest.strategy import CarryStrategy, OpenPosition
from fundarb.collect.storage import ParquetStorage
from fundarb.config import FundarbConfig
from fundarb.core.types import Market, Venue
from fundarb.research.fees import round_trip_cost_fraction
from fundarb.research.yield_calc import annualized_raw_rate, basis_fraction

log = structlog.get_logger(__name__)

_DEFAULT_LOOKBACK_PERIODS = 90  # ~30 days at 8h funding, used to estimate entry APR
_SLIPPAGE_VOLUME_FRACTION_BPS_COEFFICIENT = Decimal("2.0")  # bps of slippage per 1% of bar volume consumed


@dataclass
class Trade:
    venue: Venue
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_basis_bps: Decimal
    exit_basis_bps: Decimal
    notional: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    fee_cost: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    net_pnl_pct_of_notional: Decimal
    holding_days: Decimal
    exit_reason: str


@dataclass
class BacktestResult:
    venue: Venue
    symbol: str
    trades: list[Trade] = field(default_factory=list)

    @property
    def total_net_pnl(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal(0))

    @property
    def win_rate(self) -> Decimal:
        if not self.trades:
            return Decimal(0)
        wins = sum(1 for t in self.trades if t.net_pnl > 0)
        return Decimal(wins) / len(self.trades)

    @property
    def avg_holding_days(self) -> Decimal:
        if not self.trades:
            return Decimal(0)
        return sum((t.holding_days for t in self.trades), Decimal(0)) / len(self.trades)


class _PriceSeries:
    """Nearest-at-or-before price lookup over stored candles."""

    def __init__(self, df: pl.DataFrame) -> None:
        self._times = df["open_time"].to_list()
        self._closes = [Decimal(v) for v in df["close"].to_list()]
        self._volumes = [Decimal(v) for v in df["volume"].to_list()]

    def price_at(self, ts: datetime) -> Decimal | None:
        idx = bisect.bisect_right(self._times, ts) - 1
        if idx < 0:
            return None
        return self._closes[idx]

    def volume_at(self, ts: datetime) -> Decimal | None:
        idx = bisect.bisect_right(self._times, ts) - 1
        if idx < 0:
            return None
        return self._volumes[idx]


class BacktestEngine:
    def __init__(self, config: FundarbConfig, storage: ParquetStorage) -> None:
        self.config = config
        self.storage = storage

    def run(
        self,
        venue: Venue,
        symbol: str,
        start: datetime,
        end: datetime,
        strategy: CarryStrategy,
    ) -> BacktestResult:
        rates = [
            r
            for r in self.storage.read_funding_rates_typed(venue, symbol)
            if start <= r.funding_time <= end
        ]
        spot_prices = _PriceSeries(self.storage.read_candles(venue, symbol, Market.SPOT))
        perp_prices = _PriceSeries(self.storage.read_candles(venue, symbol, Market.PERP))
        fees_fraction = round_trip_cost_fraction(self.config.fees, venue)

        result = BacktestResult(venue=venue, symbol=symbol)
        position: OpenPosition | None = None
        entry_fee_cost = Decimal(0)
        entry_slippage_cost = Decimal(0)

        for i, rate in enumerate(rates):
            spot_p = spot_prices.price_at(rate.funding_time)
            perp_p = perp_prices.price_at(rate.funding_time)
            if spot_p is None or perp_p is None or spot_p == 0:
                continue
            basis = basis_fraction(spot_p, perp_p)

            if position is None:
                lookback = rates[max(0, i - _DEFAULT_LOOKBACK_PERIODS) : i + 1]
                raw_apr = annualized_raw_rate(lookback) * 100
                assumed_holding_years = Decimal(3) / Decimal(365)
                cost_drag_pct = (fees_fraction / assumed_holding_years) * 100
                net_apr_estimate = raw_apr - cost_drag_pct
                decision = strategy.decide_entry(
                    net_apr_estimate_pct=net_apr_estimate, basis_bps=basis * 10_000
                )
                if not decision.should_enter:
                    continue
                notional = decision.notional
                volume = spot_prices.volume_at(rate.funding_time) or Decimal(0)
                entry_slippage_cost = self._slippage_cost(notional, spot_p, volume)
                entry_fee_cost = fees_fraction / 2 * notional  # half of round trip: 2 of 4 fills
                position = OpenPosition(
                    venue=venue,
                    symbol=symbol,
                    entry_time=rate.funding_time,
                    entry_basis=basis,
                    notional=notional,
                    entry_rate_apr_pct=raw_apr,
                )
                continue

            strategy.on_funding_event(position, rate.rate)
            exit_decision = strategy.decide_exit(position, current_basis=basis, latest_rate=rate.rate)
            if not exit_decision.should_exit:
                continue

            volume = spot_prices.volume_at(rate.funding_time) or Decimal(0)
            exit_slippage_cost = self._slippage_cost(position.notional, spot_p, volume)
            exit_fee_cost = fees_fraction / 2 * position.notional
            basis_pnl = -(basis - position.entry_basis) * position.notional
            total_fee_cost = entry_fee_cost + exit_fee_cost
            total_slippage_cost = entry_slippage_cost + exit_slippage_cost
            net_pnl = position.cumulative_funding_pnl + basis_pnl - total_fee_cost - total_slippage_cost
            holding_days = Decimal((rate.funding_time - position.entry_time).total_seconds()) / Decimal(86400)

            result.trades.append(
                Trade(
                    venue=venue,
                    symbol=symbol,
                    entry_time=position.entry_time,
                    exit_time=rate.funding_time,
                    entry_basis_bps=position.entry_basis * 10_000,
                    exit_basis_bps=basis * 10_000,
                    notional=position.notional,
                    funding_pnl=position.cumulative_funding_pnl,
                    basis_pnl=basis_pnl,
                    fee_cost=total_fee_cost,
                    slippage_cost=total_slippage_cost,
                    net_pnl=net_pnl,
                    net_pnl_pct_of_notional=(net_pnl / position.notional * 100) if position.notional else Decimal(0),
                    holding_days=holding_days,
                    exit_reason=exit_decision.reason,
                )
            )
            position = None

        return result

    @staticmethod
    def _slippage_cost(notional: Decimal, price: Decimal, bar_volume_base: Decimal) -> Decimal:
        """Slippage grows with the fraction of the bar's traded volume this
        order would consume — a crude but directionally correct proxy for
        walking the book when true depth history isn't stored.
        """
        bar_volume_quote = bar_volume_base * price
        if bar_volume_quote <= 0:
            return notional * Decimal("0.001")  # no volume data: conservative 10bps floor
        fraction = notional / bar_volume_quote
        slippage_bps = fraction * 100 * _SLIPPAGE_VOLUME_FRACTION_BPS_COEFFICIENT
        return notional * slippage_bps / 10_000
