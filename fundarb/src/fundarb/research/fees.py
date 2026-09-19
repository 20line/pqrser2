"""Fee schedule and full-round-trip cost calculation.

A full cycle is four fills, not one: open spot, open perp, close spot,
close perp — each at its own maker/taker rate. Counting one fee instead of
four is one of the three mistakes the spec calls out explicitly.
"""

from __future__ import annotations

from decimal import Decimal

from fundarb.config import FeesConfig
from fundarb.core.types import Market, Venue

_BPS = Decimal(10_000)


def fee_rate(fees: FeesConfig, venue: Venue, market: Market, *, maker: bool) -> Decimal:
    """Fee as a fraction of notional (not bps) for one fill."""
    schedule = fees.for_venue(venue)
    if market is Market.SPOT:
        bps = schedule.spot_maker_bps if maker else schedule.spot_taker_bps
    else:
        bps = schedule.perp_maker_bps if maker else schedule.perp_taker_bps
    return bps / _BPS


def round_trip_cost_fraction(
    fees: FeesConfig,
    venue: Venue,
    *,
    entry_maker: bool = False,
    exit_maker: bool = False,
) -> Decimal:
    """Sum of all four fills' fees, as a fraction of notional. Entry and
    exit are usually both taker (market orders, to guarantee the delta-
    neutral fill), but the maker flags let research explore limit-order
    variants.
    """
    entry = fee_rate(fees, venue, Market.SPOT, maker=entry_maker) + fee_rate(
        fees, venue, Market.PERP, maker=entry_maker
    )
    exit_ = fee_rate(fees, venue, Market.SPOT, maker=exit_maker) + fee_rate(
        fees, venue, Market.PERP, maker=exit_maker
    )
    return entry + exit_
