"""Append-only trade ledger, one row per closed position. Exists from day
one per the spec's tax/reporting requirement ("Система должна с первого
дня вести полный журнал сделок и начислений в формате, пригодном для
отчётности") — reconstructing this after the fact from structlog output
alone is exactly the "expensive" the spec warns about.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import polars as pl

from fundarb.core.errors import StorageError
from fundarb.core.types import Venue

_SCHEMA = {
    "venue": pl.Utf8,
    "symbol": pl.Utf8,
    "entry_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "exit_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "entry_basis": pl.Utf8,
    "exit_basis": pl.Utf8,
    "notional": pl.Utf8,
    "funding_pnl": pl.Utf8,
    "basis_pnl": pl.Utf8,
    "realized_pnl": pl.Utf8,
    "exit_reason": pl.Utf8,
}


@dataclass(frozen=True)
class LedgerEntry:
    venue: Venue
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_basis: Decimal
    exit_basis: Decimal
    notional: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    realized_pnl: Decimal
    exit_reason: str


class TradeLedger:
    def __init__(self, data_dir: str | Path) -> None:
        self.ledger_dir = Path(data_dir) / "ledger"

    def _path(self) -> Path:
        return self.ledger_dir / "trades.parquet"

    def append(self, entry: LedgerEntry) -> None:
        row = pl.DataFrame(
            [
                {
                    "venue": entry.venue.value,
                    "symbol": entry.symbol,
                    "entry_time": entry.entry_time,
                    "exit_time": entry.exit_time,
                    "entry_basis": str(entry.entry_basis),
                    "exit_basis": str(entry.exit_basis),
                    "notional": str(entry.notional),
                    "funding_pnl": str(entry.funding_pnl),
                    "basis_pnl": str(entry.basis_pnl),
                    "realized_pnl": str(entry.realized_pnl),
                    "exit_reason": entry.exit_reason,
                }
            ],
            schema=_SCHEMA,
        )
        path = self._path()
        merged = pl.concat([pl.read_parquet(path), row]) if path.exists() else row
        try:
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self.ledger_dir / f".tmp-{uuid.uuid4().hex}.parquet"
            merged.write_parquet(tmp_path)
            os.replace(tmp_path, path)
        except OSError as exc:
            raise StorageError(f"failed writing trade ledger {path}: {exc}") from exc

    def read_all(self) -> pl.DataFrame:
        path = self._path()
        if not path.exists():
            return pl.DataFrame(schema=_SCHEMA)
        return pl.read_parquet(path).sort("exit_time")
