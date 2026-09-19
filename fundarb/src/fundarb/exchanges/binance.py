from __future__ import annotations

from fundarb.core.types import Venue
from fundarb.exchanges._ccxt_base import CCXTExchangeAdapter


class BinanceAdapter(CCXTExchangeAdapter):
    venue = Venue.BINANCE
    ccxt_id = "binance"
    default_funding_interval_hours = 8

    def __init__(self, *, api_key: str = "", api_secret: str = "", testnet: bool = True) -> None:
        super().__init__(api_key=api_key, api_secret=api_secret, testnet=testnet)
