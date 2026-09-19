from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fundarb.collect.storage import ParquetStorage
from fundarb.core.models import Candle, FundingRate
from fundarb.core.types import Market, Venue

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rate(offset_hours: int, rate: str = "0.0001") -> FundingRate:
    return FundingRate(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        funding_time=_T0 + timedelta(hours=offset_hours),
        rate=Decimal(rate),
        interval_hours=8,
        mark_price=Decimal("50000"),
    )


def _candle(offset_hours: int) -> Candle:
    return Candle(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        market=Market.SPOT,
        interval="1h",
        open_time=_T0 + timedelta(hours=offset_hours),
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49900"),
        close=Decimal("50050"),
        volume=Decimal("100"),
    )


def test_write_and_read_funding_rates_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    rows = [_rate(i * 8) for i in range(5)]
    written = storage.write_funding_rates(rows)
    assert written == 5

    df = storage.read_funding_rates(Venue.BINANCE, "BTC/USDT")
    assert df.height == 5

    typed = storage.read_funding_rates_typed(Venue.BINANCE, "BTC/USDT")
    assert len(typed) == 5
    assert typed[0].rate == Decimal("0.0001")
    assert typed == sorted(typed, key=lambda r: r.funding_time)


def test_write_funding_rates_is_idempotent_on_rerun(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    rows = [_rate(i * 8) for i in range(5)]
    storage.write_funding_rates(rows)
    # re-run with the same rows: must not duplicate
    second_written = storage.write_funding_rates(rows)
    assert second_written == 0
    df = storage.read_funding_rates(Venue.BINANCE, "BTC/USDT")
    assert df.height == 5


def test_write_funding_rates_merges_overlapping_ranges(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.write_funding_rates([_rate(i * 8) for i in range(5)])
    # overlap: rows 3,4 already exist, 5,6 are new
    new_written = storage.write_funding_rates([_rate(i * 8) for i in range(3, 7)])
    assert new_written == 2
    df = storage.read_funding_rates(Venue.BINANCE, "BTC/USDT")
    assert df.height == 7


def test_funding_rates_partition_by_year(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    row_2025 = FundingRate(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        funding_time=datetime(2025, 12, 31, tzinfo=timezone.utc),
        rate=Decimal("0.0001"),
        interval_hours=8,
        mark_price=Decimal("50000"),
    )
    row_2026 = _rate(0)
    storage.write_funding_rates([row_2025, row_2026])

    part_2025 = storage.data_dir / "funding" / "venue=binance" / "symbol=BTCUSDT" / "year=2025" / "part.parquet"
    part_2026 = storage.data_dir / "funding" / "venue=binance" / "symbol=BTCUSDT" / "year=2026" / "part.parquet"
    assert part_2025.exists()
    assert part_2026.exists()

    all_rows = storage.read_funding_rates(Venue.BINANCE, "BTC/USDT")
    assert all_rows.height == 2


def test_atomic_write_leaves_no_temp_files(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.write_funding_rates([_rate(0)])
    part_dir = storage.data_dir / "funding" / "venue=binance" / "symbol=BTCUSDT" / "year=2026"
    tmp_files = list(part_dir.glob(".tmp-*"))
    assert tmp_files == []


def test_candles_round_trip_and_idempotency(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    rows = [_candle(i) for i in range(24)]
    written = storage.write_candles(rows)
    assert written == 24
    second = storage.write_candles(rows)
    assert second == 0

    df = storage.read_candles(Venue.BINANCE, "BTC/USDT", Market.SPOT)
    assert df.height == 24


def test_read_missing_symbol_returns_empty_frame(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    df = storage.read_funding_rates(Venue.BINANCE, "DOES/NOTEXIST")
    assert df.height == 0
    typed = storage.read_funding_rates_typed(Venue.BINANCE, "DOES/NOTEXIST")
    assert typed == []
