from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fundarb.core.errors import StorageError
from fundarb.core.types import Venue
from fundarb.execution.ledger import LedgerEntry, TradeLedger

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entry(exit_time_offset_sec: int = 0) -> LedgerEntry:
    return LedgerEntry(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        entry_time=_T0,
        exit_time=_T0 + timedelta(seconds=exit_time_offset_sec),
        entry_basis=Decimal("0.001"),
        exit_basis=Decimal("0.0005"),
        notional=Decimal("5000"),
        funding_pnl=Decimal("30"),
        basis_pnl=Decimal("2.5"),
        realized_pnl=Decimal("32.5"),
        exit_reason="exit_leg1",
    )


def test_append_and_read_round_trip(tmp_path) -> None:
    ledger = TradeLedger(tmp_path)
    ledger.append(_entry())
    rows = ledger.read_all()
    assert rows.height == 1
    row = rows.row(0, named=True)
    assert row["symbol"] == "BTC/USDT"
    assert Decimal(row["realized_pnl"]) == Decimal("32.5")


def test_multiple_appends_accumulate(tmp_path) -> None:
    ledger = TradeLedger(tmp_path)
    ledger.append(_entry(0))
    ledger.append(_entry(10))
    ledger.append(_entry(20))
    rows = ledger.read_all()
    assert rows.height == 3


def test_read_all_on_empty_ledger_returns_empty_frame(tmp_path) -> None:
    ledger = TradeLedger(tmp_path)
    rows = ledger.read_all()
    assert rows.height == 0


def test_corrupt_existing_ledger_file_raises_storage_error_not_raw_polars_error(tmp_path) -> None:
    """Regression test: pl.read_parquet(path) used to run outside the
    try/except in TradeLedger.append, so a corrupt/unreadable existing
    file raised a raw polars exception instead of StorageError — which
    live_runner.py's `except StorageError` in _execute_close doesn't
    catch, silently losing the explicit backfill-instruction alert.
    """
    ledger = TradeLedger(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "trades.parquet").write_bytes(b"not a real parquet file")

    with pytest.raises(StorageError):
        ledger.append(_entry())
