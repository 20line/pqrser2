from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fundarb.core.models import FundingRate
from fundarb.core.types import Venue
from fundarb.research.fees import round_trip_cost_fraction
from fundarb.research.yield_calc import (
    annualized_raw_rate,
    basis_fraction,
    compute_stability,
    net_apr,
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rate(offset_hours: int, rate: str, interval_hours: int = 8) -> FundingRate:
    return FundingRate(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        funding_time=_T0 + timedelta(hours=offset_hours),
        rate=Decimal(rate),
        interval_hours=interval_hours,
        mark_price=Decimal("50000"),
    )


def test_annualized_raw_rate_matches_hand_calc_for_constant_interval() -> None:
    rates = [_rate(i * 8, "0.0001", interval_hours=8) for i in range(10)]
    result = annualized_raw_rate(rates)
    expected = Decimal("0.0001") * Decimal(8760) / 8
    assert result == expected


def test_annualized_raw_rate_handles_interval_change_mid_history() -> None:
    """The spec's #1 pitfall: assuming a fixed interval. A history that
    switches from 8h to 4h contracts must not silently use one global h.
    """
    rates = [_rate(i * 8, "0.0001", interval_hours=8) for i in range(5)]
    rates += [_rate(40 + i * 4, "0.0001", interval_hours=4) for i in range(5)]
    result = annualized_raw_rate(rates)
    per_record = [Decimal("0.0001") * Decimal(8760) / r.interval_hours for r in rates]
    expected = sum(per_record, Decimal(0)) / len(per_record)
    assert result == expected
    # sanity: this must differ from naively assuming h=8 throughout
    naive = Decimal("0.0001") * Decimal(8760) / 8
    assert result != naive


def test_annualized_raw_rate_empty() -> None:
    assert annualized_raw_rate([]) == Decimal(0)


def test_basis_fraction() -> None:
    assert basis_fraction(Decimal("100"), Decimal("101")) == Decimal("0.01")
    assert basis_fraction(Decimal("100"), Decimal("99")) == Decimal("-0.01")
    assert basis_fraction(Decimal("0"), Decimal("100")) == Decimal(0)


def test_net_apr_subtracts_annualized_costs_and_basis_drag() -> None:
    rates = [_rate(i * 8, "0.0001", interval_hours=8) for i in range(30)]
    raw = annualized_raw_rate(rates)
    cost = Decimal("0.006")  # 60bps round trip
    holding = Decimal(10) / Decimal(365)
    entry_basis = Decimal("0.002")
    exit_basis = Decimal("0.001")
    result = net_apr(
        rates,
        round_trip_cost_fraction=cost,
        holding_period_years=holding,
        entry_basis=entry_basis,
        exit_basis=exit_basis,
    )
    expected = raw - cost / holding - (exit_basis - entry_basis)
    assert result == expected


def test_net_apr_rejects_nonpositive_holding_period() -> None:
    import pytest

    with pytest.raises(ValueError):
        net_apr(
            [],
            round_trip_cost_fraction=Decimal("0.01"),
            holding_period_years=Decimal(0),
            entry_basis=Decimal(0),
            exit_basis=Decimal(0),
        )


def test_round_trip_cost_counts_all_four_fills(fees_config) -> None:
    """The spec's #3 pitfall: counting one fee instead of four (open+close
    x spot+perp).
    """
    cost = round_trip_cost_fraction(fees_config, Venue.BINANCE)
    schedule = fees_config.binance
    expected = (
        schedule.spot_taker_bps
        + schedule.perp_taker_bps
        + schedule.spot_taker_bps
        + schedule.perp_taker_bps
    ) / Decimal(10_000)
    assert cost == expected


def test_compute_stability_negative_share_and_drawdown() -> None:
    rates = [
        _rate(0, "0.0002"),
        _rate(8, "0.0002"),
        _rate(16, "-0.0001"),
        _rate(24, "-0.0001"),
        _rate(32, "0.0003"),
    ]
    stats = compute_stability(rates)
    assert stats.sample_size == 5
    assert stats.negative_period_share == Decimal(2) / 5
    # cumulative: 0.0002, 0.0004, 0.0003, 0.0002, 0.0005 -> peak 0.0004, trough 0.0002 -> dd 0.0002
    assert stats.max_funding_drawdown == Decimal("0.0002")


def test_compute_stability_empty() -> None:
    stats = compute_stability([])
    assert stats.sample_size == 0
    assert stats.mean_rate == Decimal(0)
