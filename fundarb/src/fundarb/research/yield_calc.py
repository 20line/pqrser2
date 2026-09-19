"""Net-return calculation — the answer that decides whether the project has
an edge at all (Phase 2 go/no-go). Implements:

    APR_net = (8760/h) * r̄  -  (C_in + C_out) / T_years  -  ΔB

with two corrections the spec calls out as easy to get wrong:

  * the funding interval `h` is NOT assumed constant — each FundingRate
    carries the interval it actually had when recorded, so a history that
    spans an interval change (e.g. 8h -> 4h) still annualizes correctly
    record by record instead of using one global `h`.
  * costs are the full four-fill round trip (see fees.py), not one fee.

Per the spec's literal formula, the basis drag `ΔB` is subtracted as an
absolute (realized-once) term, not divided by the holding period — unlike
the fee term, which is annualized via `/T_years`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fundarb.core.models import FundingRate

_HOURS_PER_YEAR = Decimal(8760)


def basis_fraction(spot_price: Decimal, perp_price: Decimal) -> Decimal:
    """(perp - spot) / spot. Positive means perp trades above spot."""
    if spot_price == 0:
        return Decimal(0)
    return (perp_price - spot_price) / spot_price


def annualized_raw_rate(rates: list[FundingRate]) -> Decimal:
    """Mean of each record's own annualized rate — robust to interval
    changes within the history.
    """
    if not rates:
        return Decimal(0)
    total = sum((r.rate * _HOURS_PER_YEAR / r.interval_hours for r in rates), Decimal(0))
    return total / len(rates)


def net_apr(
    rates: list[FundingRate],
    *,
    round_trip_cost_fraction: Decimal,
    holding_period_years: Decimal,
    entry_basis: Decimal,
    exit_basis: Decimal,
) -> Decimal:
    if holding_period_years <= 0:
        raise ValueError("holding_period_years must be positive")
    raw = annualized_raw_rate(rates)
    cost_term = round_trip_cost_fraction / holding_period_years
    delta_basis = exit_basis - entry_basis
    return raw - cost_term - delta_basis


@dataclass(frozen=True)
class RateStabilityStats:
    mean_rate: Decimal
    median_rate: Decimal
    negative_period_share: Decimal
    max_funding_drawdown: Decimal  # max peak-to-trough dip of cumulative funding, as a fraction
    sample_size: int
    span_days: int


def compute_stability(rates: list[FundingRate]) -> RateStabilityStats:
    if not rates:
        return RateStabilityStats(
            mean_rate=Decimal(0),
            median_rate=Decimal(0),
            negative_period_share=Decimal(0),
            max_funding_drawdown=Decimal(0),
            sample_size=0,
            span_days=0,
        )
    ordered = sorted(rates, key=lambda r: r.funding_time)
    values = [r.rate for r in ordered]
    n = len(values)

    mean_rate = sum(values, Decimal(0)) / n
    sorted_values = sorted(values)
    mid = n // 2
    median_rate = (
        sorted_values[mid] if n % 2 == 1 else (sorted_values[mid - 1] + sorted_values[mid]) / 2
    )
    negative_share = Decimal(sum(1 for v in values if v < 0)) / n

    cumulative = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    for v in values:
        cumulative += v
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    span_days = (ordered[-1].funding_time.date() - ordered[0].funding_time.date()).days

    return RateStabilityStats(
        mean_rate=mean_rate,
        median_rate=median_rate,
        negative_period_share=negative_share,
        max_funding_drawdown=max_drawdown,
        sample_size=n,
        span_days=span_days,
    )
