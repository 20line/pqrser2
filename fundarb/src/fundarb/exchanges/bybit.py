from __future__ import annotations

from fundarb.core.types import Venue
from fundarb.exchanges._ccxt_base import CCXTExchangeAdapter


class BybitAdapter(CCXTExchangeAdapter):
    venue = Venue.BYBIT
    ccxt_id = "bybit"
    default_funding_interval_hours = 8

    def __init__(self, *, api_key: str = "", api_secret: str = "", testnet: bool = True) -> None:
        super().__init__(api_key=api_key, api_secret=api_secret, testnet=testnet)
