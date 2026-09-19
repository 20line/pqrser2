"""Parquet storage, partitioned by venue/symbol/year so research code reads
only the slice it needs instead of loading the whole history into memory.
Writes are atomic (temp file + rename) so a crash mid-write never leaves a
corrupt partition that later reads choke on.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

import polars as pl

from fundarb.core.errors import StorageError
from fundarb.core.models import Candle, FundingRate
from fundarb.core.types import Market, Venue

_FUNDING_SCHEMA = {
    "venue": pl.Utf8,
    "symbol": pl.Utf8,
    "funding_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "rate": pl.Utf8,  # Decimal stored as string, cast back on read
    "interval_hours": pl.Int32,
    "mark_price": pl.Utf8,
}

_PRICE_SCHEMA = {
    "venue": pl.Utf8,
    "symbol": pl.Utf8,
    "market": pl.Utf8,
    "interval": pl.Utf8,
    "open_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "open": pl.Utf8,
    "high": pl.Utf8,
    "low": pl.Utf8,
    "close": pl.Utf8,
    "volume": pl.Utf8,
}


class ParquetStorage:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)

    # ---- funding --------------------------------------------------------

    def _funding_partition_dir(self, venue: Venue, symbol: str, year: int) -> Path:
        safe_symbol = symbol.replace("/", "")
        return self.data_dir / "funding" / f"venue={venue.value}" / f"symbol={safe_symbol}" / f"year={year}"

    def write_funding_rates(self, rows: list[FundingRate]) -> int:
        """Idempotent: merges new rows into the existing partition, dedupes
        on (venue, symbol, funding_time), and rewrites atomically. Returns
        the number of genuinely new rows written.
        """
        if not rows:
            return 0
        by_partition: dict[tuple[Venue, str, int], list[FundingRate]] = {}
        for r in rows:
            key = (r.venue, r.symbol, r.funding_time.year)
            by_partition.setdefault(key, []).append(r)

        new_count = 0
        for (venue, symbol, year), part_rows in by_partition.items():
            new_df = pl.DataFrame(
                [
                    {
                        "venue": r.venue.value,
                        "symbol": r.symbol,
                        "funding_time": r.funding_time,
                        "rate": str(r.rate),
                        "interval_hours": r.interval_hours,
                        "mark_price": str(r.mark_price),
                    }
                    for r in part_rows
                ],
                schema=_FUNDING_SCHEMA,
            )
            part_dir = self._funding_partition_dir(venue, symbol, year)
            part_file = part_dir / "part.parquet"
            before = 0
            if part_file.exists():
                existing = pl.read_parquet(part_file)
                before = existing.height
                merged = pl.concat([existing, new_df])
            else:
                merged = new_df
            merged = merged.unique(subset=["venue", "symbol", "funding_time"], keep="last").sort(
                "funding_time"
            )
            self._atomic_write(merged, part_dir, part_file)
            new_count += merged.height - before
        return new_count

    def read_funding_rates(
        self, venue: Venue, symbol: str, year: int | None = None
    ) -> pl.DataFrame:
        safe_symbol = symbol.replace("/", "")
        base = self.data_dir / "funding" / f"venue={venue.value}" / f"symbol={safe_symbol}"
        if year is not None:
            files = [base / f"year={year}" / "part.parquet"]
        else:
            files = sorted(base.glob("year=*/part.parquet")) if base.exists() else []
        files = [f for f in files if f.exists()]
        if not files:
            return pl.DataFrame(schema=_FUNDING_SCHEMA)
        return pl.concat([pl.read_parquet(f) for f in files]).sort("funding_time")

    def read_funding_rates_typed(
        self, venue: Venue, symbol: str, year: int | None = None
    ) -> list[FundingRate]:
        df = self.read_funding_rates(venue, symbol, year)
        return [
            FundingRate(
                venue=Venue(row["venue"]),
                symbol=row["symbol"],
                funding_time=row["funding_time"],
                rate=Decimal(row["rate"]),
                interval_hours=row["interval_hours"],
                mark_price=Decimal(row["mark_price"]),
            )
            for row in df.iter_rows(named=True)
        ]

    # ---- prices -----------------------------------------------------------

    def _price_partition_dir(self, venue: Venue, symbol: str, market: Market, year: int) -> Path:
        safe_symbol = symbol.replace("/", "")
        return (
            self.data_dir
            / "prices"
            / f"venue={venue.value}"
            / f"symbol={safe_symbol}"
            / f"market={market.value}"
            / f"year={year}"
        )

    def write_candles(self, rows: list[Candle]) -> int:
        if not rows:
            return 0
        by_partition: dict[tuple[Venue, str, Market, int], list[Candle]] = {}
        for r in rows:
            key = (r.venue, r.symbol, r.market, r.open_time.year)
            by_partition.setdefault(key, []).append(r)

        new_count = 0
        for (venue, symbol, market, year), part_rows in by_partition.items():
            new_df = pl.DataFrame(
                [
                    {
                        "venue": r.venue.value,
                        "symbol": r.symbol,
                        "market": r.market.value,
                        "interval": r.interval,
                        "open_time": r.open_time,
                        "open": str(r.open),
                        "high": str(r.high),
                        "low": str(r.low),
                        "close": str(r.close),
                        "volume": str(r.volume),
                    }
                    for r in part_rows
                ],
                schema=_PRICE_SCHEMA,
            )
            part_dir = self._price_partition_dir(venue, symbol, market, year)
            part_file = part_dir / "part.parquet"
            before = 0
            if part_file.exists():
                existing = pl.read_parquet(part_file)
                before = existing.height
                merged = pl.concat([existing, new_df])
            else:
                merged = new_df
            merged = merged.unique(
                subset=["venue", "symbol", "market", "interval", "open_time"], keep="last"
            ).sort("open_time")
            self._atomic_write(merged, part_dir, part_file)
            new_count += merged.height - before
        return new_count

    def read_candles(
        self, venue: Venue, symbol: str, market: Market, year: int | None = None
    ) -> pl.DataFrame:
        safe_symbol = symbol.replace("/", "")
        base = self.data_dir / "prices" / f"venue={venue.value}" / f"symbol={safe_symbol}" / f"market={market.value}"
        if year is not None:
            files = [base / f"year={year}" / "part.parquet"]
        else:
            files = sorted(base.glob("year=*/part.parquet")) if base.exists() else []
        files = [f for f in files if f.exists()]
        if not files:
            return pl.DataFrame(schema=_PRICE_SCHEMA)
        return pl.concat([pl.read_parquet(f) for f in files]).sort("open_time")

    # ---- shared -------------------------------------------------------

    @staticmethod
    def _atomic_write(df: pl.DataFrame, part_dir: Path, final_path: Path) -> None:
        try:
            part_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = part_dir / f".tmp-{uuid.uuid4().hex}.parquet"
            df.write_parquet(tmp_path)
            os.replace(tmp_path, final_path)
        except OSError as exc:
            raise StorageError(f"failed writing {final_path}: {exc}") from exc
