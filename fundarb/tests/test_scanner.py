from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fundarb.collect.storage import ParquetStorage
from fundarb.core.models import FundingRate, Instrument
from fundarb.core.types import Venue
from fundarb.scanner.scanner import Scanner

_SYMBOL = "BTC/USDT"
_VENUE = Venue.BINANCE


def _instrument(spot_volume: str, perp_volume: str) -> Instrument:
    return Instrument(
        venue=_VENUE,
        symbol=_SYMBOL,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT:USDT",
        funding_interval_hours=8,
        price_step=Decimal("0.01"),
        qty_step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        listed_at=datetime.now(timezone.utc) - timedelta(days=365),
        spot_quote_volume_24h_usd=Decimal(spot_volume),
        perp_quote_volume_24h_usd=Decimal(perp_volume),
    )


def _seed_healthy_history(storage: ParquetStorage) -> None:
    now = datetime.now(timezone.utc)
    storage.write_funding_rates(
        [
            FundingRate(
                venue=_VENUE,
                symbol=_SYMBOL,
                funding_time=now - timedelta(hours=8 * i),
                rate=Decimal("0.0006"),
                interval_hours=8,
                mark_price=Decimal("50000"),
            )
            for i in range(1, 300)
        ]
    )


def test_rejects_pair_with_thin_perp_volume_despite_thick_spot_volume(tmp_path, fundarb_config) -> None:
    storage = ParquetStorage(tmp_path)
    _seed_healthy_history(storage)
    scanner = Scanner(fundarb_config, storage)

    instrument = _instrument(spot_volume="100000000", perp_volume="1000")  # perp far below the $20M bar
    candidates = scanner.scan([instrument])

    assert len(candidates) == 1
    assert not candidates[0].eligible
    assert "perp 24h volume" in candidates[0].rejected_reason


def test_rejects_pair_with_thin_spot_volume_despite_thick_perp_volume(tmp_path, fundarb_config) -> None:
    storage = ParquetStorage(tmp_path)
    _seed_healthy_history(storage)
    scanner = Scanner(fundarb_config, storage)

    instrument = _instrument(spot_volume="1000", perp_volume="100000000")
    candidates = scanner.scan([instrument])

    assert len(candidates) == 1
    assert not candidates[0].eligible
    assert "spot 24h volume" in candidates[0].rejected_reason


def test_accepts_pair_when_both_legs_are_liquid(tmp_path, fundarb_config) -> None:
    storage = ParquetStorage(tmp_path)
    _seed_healthy_history(storage)
    scanner = Scanner(fundarb_config, storage)

    instrument = _instrument(spot_volume="100000000", perp_volume="100000000")
    candidates = scanner.scan([instrument])

    assert len(candidates) == 1
    assert candidates[0].eligible, candidates[0].rejected_reason
