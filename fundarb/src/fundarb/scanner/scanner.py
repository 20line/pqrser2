"""Walks the instrument universe on both venues and produces a ranked list
of candidates. Universe policy is "broad coverage, cut by liquidity" (see
config/config.yaml `universe.mode: volume_filter`) rather than a hand-picked
shortlist of large-cap pairs: any pair that clears the volume and age bars
is eligible, so the scanner — not a static list — decides what counts as
"liquid enough".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from fundarb.collect.storage import ParquetStorage
from fundarb.config import FundarbConfig
from fundarb.core.models import Instrument, Quote
from fundarb.core.types import Market, Venue
from fundarb.research.fees import round_trip_cost_fraction
from fundarb.research.yield_calc import RateStabilityStats, annualized_raw_rate, basis_fraction, compute_stability

_DAYS_PER_YEAR = Decimal(365)


@dataclass(frozen=True)
class Candidate:
    venue: Venue
    symbol: str
    current_rate: Decimal
    interval_hours: int
    raw_apr_pct: Decimal
    net_apr_estimate_pct: Decimal
    stability: RateStabilityStats
    spot_quote_volume_24h_usd: Decimal | None
    perp_quote_volume_24h_usd: Decimal | None
    basis_bps: Decimal | None
    rejected_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.rejected_reason is None


class Scanner:
    def __init__(
        self,
        config: FundarbConfig,
        storage: ParquetStorage,
        *,
        assumed_holding_days: int = 3,
    ) -> None:
        self.config = config
        self.storage = storage
        self.assumed_holding_years = Decimal(assumed_holding_days) / _DAYS_PER_YEAR

    def scan(
        self,
        instruments: list[Instrument],
        quotes: dict[tuple[Venue, str, Market], Quote] | None = None,
    ) -> list[Candidate]:
        quotes = quotes or {}
        candidates = [self._evaluate(inst, quotes) for inst in instruments]
        return sorted(
            candidates,
            key=lambda c: (c.eligible, c.net_apr_estimate_pct),
            reverse=True,
        )

    def _evaluate(
        self,
        instrument: Instrument,
        quotes: dict[tuple[Venue, str, Market], Quote],
    ) -> Candidate:
        uni = self.config.universe
        entry = self.config.entry

        rejected = self._universe_rejection(instrument)

        rates = self.storage.read_funding_rates_typed(instrument.venue, instrument.symbol)
        stability = compute_stability(rates)
        raw_apr = annualized_raw_rate(rates) * 100

        basis_bps = None
        spot_quote = quotes.get((instrument.venue, instrument.symbol, Market.SPOT))
        perp_quote = quotes.get((instrument.venue, instrument.symbol, Market.PERP))
        if spot_quote and perp_quote:
            basis_bps = basis_fraction(spot_quote.mid, perp_quote.mid) * 10_000
            if rejected is None and abs(basis_bps) > uni.max_spread_bps:
                rejected = f"basis {basis_bps:.1f}bps exceeds max_spread_bps={uni.max_spread_bps}"

        if rejected is None and not rates:
            rejected = "no funding history collected yet"
        elif rejected is None:
            if stability.span_days < entry.min_history_days:
                rejected = f"history span {stability.span_days}d < min_history_days={entry.min_history_days}"
            elif stability.negative_period_share > entry.max_negative_period_share:
                rejected = (
                    f"negative period share {stability.negative_period_share:.2%} "
                    f"> max_negative_period_share={entry.max_negative_period_share:.2%}"
                )
            elif stability.max_funding_drawdown * 100 > entry.max_funding_drawdown_pct:
                rejected = (
                    f"funding drawdown {stability.max_funding_drawdown * 100:.2f}% "
                    f"> max_funding_drawdown_pct={entry.max_funding_drawdown_pct}%"
                )

        fees_fraction = round_trip_cost_fraction(self.config.fees, instrument.venue)
        cost_drag_pct = (fees_fraction / self.assumed_holding_years) * 100
        net_apr_estimate = raw_apr - cost_drag_pct

        if rejected is None and net_apr_estimate < entry.min_net_apr_pct:
            rejected = (
                f"net APR estimate {net_apr_estimate:.2f}% < min_net_apr_pct={entry.min_net_apr_pct}%"
            )

        current_rate = rates[-1].rate if rates else Decimal(0)

        return Candidate(
            venue=instrument.venue,
            symbol=instrument.symbol,
            current_rate=current_rate,
            interval_hours=instrument.funding_interval_hours,
            raw_apr_pct=raw_apr,
            net_apr_estimate_pct=net_apr_estimate,
            stability=stability,
            spot_quote_volume_24h_usd=instrument.spot_quote_volume_24h_usd,
            perp_quote_volume_24h_usd=instrument.perp_quote_volume_24h_usd,
            basis_bps=basis_bps,
            rejected_reason=rejected,
        )

    def _universe_rejection(self, instrument: Instrument) -> str | None:
        """Both legs need to clear the liquidity bar: the perp leg is
        usually the one that actually eats slippage on entry/exit (it's
        where reduce/increase happens for the short side), so checking
        spot volume alone gives a false sense of safety.
        """
        uni = self.config.universe
        if instrument.symbol in uni.exclude_symbols:
            return "excluded by config"
        if (
            instrument.spot_quote_volume_24h_usd is not None
            and instrument.spot_quote_volume_24h_usd < uni.min_24h_quote_volume_usd
        ):
            return (
                f"spot 24h volume ${instrument.spot_quote_volume_24h_usd:,.0f} "
                f"< min_24h_quote_volume_usd=${uni.min_24h_quote_volume_usd:,.0f}"
            )
        if (
            instrument.perp_quote_volume_24h_usd is not None
            and instrument.perp_quote_volume_24h_usd < uni.min_24h_quote_volume_usd
        ):
            return (
                f"perp 24h volume ${instrument.perp_quote_volume_24h_usd:,.0f} "
                f"< min_24h_quote_volume_usd=${uni.min_24h_quote_volume_usd:,.0f}"
            )
        if instrument.listed_at is not None:
            age_days = (datetime.now(timezone.utc) - instrument.listed_at).days
            if age_days < uni.min_contract_age_days:
                return f"contract age {age_days}d < min_contract_age_days={uni.min_contract_age_days}"
        return None
