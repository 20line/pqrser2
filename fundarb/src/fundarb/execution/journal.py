"""Persists the one piece of state that `execution/reconcile.py` cannot
recover from the exchange: entry_time, entry_basis, and the funding/streak
counters a strategy needs to decide when to exit. Reconciliation still
wins for spot_qty/perp_qty (the exchange is always the source of truth for
*how much* is held) — the journal only fills in *since when* and *at what
basis*, which the exchange doesn't track for us.

Without this, a restart mid-position forces `cli.py` to approximate
entry_time as "now" and entry_basis as the current basis — which silently
resets the fixed_profit target and the rate_reversal streak counter,
either delaying an exit that should have already fired or triggering one
that shouldn't have.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import structlog

from fundarb.backtest.strategy import OpenPosition
from fundarb.core.errors import StorageError
from fundarb.core.types import Venue

log = structlog.get_logger(__name__)


class PositionJournal:
    def __init__(self, data_dir: str | Path) -> None:
        self.state_dir = Path(data_dir) / "state"

    def _path(self, venue: Venue, symbol: str) -> Path:
        safe_symbol = symbol.replace("/", "")
        return self.state_dir / f"{venue.value}_{safe_symbol}.json"

    def save(self, position: OpenPosition) -> None:
        payload = {
            "venue": position.venue.value,
            "symbol": position.symbol,
            "entry_time": position.entry_time.isoformat(),
            "entry_basis": str(position.entry_basis),
            "notional": str(position.notional),
            "entry_rate_apr_pct": str(position.entry_rate_apr_pct),
            "cumulative_funding_pnl": str(position.cumulative_funding_pnl),
            "consecutive_negative_periods": position.consecutive_negative_periods,
            "periods_held": position.periods_held,
            "spot_qty": str(position.spot_qty),
            "perp_qty": str(position.perp_qty),
            "last_applied_funding_time": (
                position.last_applied_funding_time.isoformat()
                if position.last_applied_funding_time
                else None
            ),
        }
        path = self._path(position.venue, position.symbol)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_dir / f".tmp-{uuid.uuid4().hex}.json"
            tmp_path.write_text(json.dumps(payload, indent=2))
            os.replace(tmp_path, path)
        except OSError as exc:
            raise StorageError(f"failed writing position journal {path}: {exc}") from exc

    def load(self, venue: Venue, symbol: str) -> OpenPosition | None:
        path = self._path(venue, symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("failed reading position journal, ignoring", path=str(path), error=str(exc))
            return None
        return OpenPosition(
            venue=Venue(payload["venue"]),
            symbol=payload["symbol"],
            entry_time=datetime.fromisoformat(payload["entry_time"]),
            entry_basis=Decimal(payload["entry_basis"]),
            notional=Decimal(payload["notional"]),
            entry_rate_apr_pct=Decimal(payload["entry_rate_apr_pct"]),
            cumulative_funding_pnl=Decimal(payload["cumulative_funding_pnl"]),
            consecutive_negative_periods=payload["consecutive_negative_periods"],
            periods_held=payload["periods_held"],
            spot_qty=Decimal(payload["spot_qty"]),
            perp_qty=Decimal(payload["perp_qty"]),
            last_applied_funding_time=(
                datetime.fromisoformat(payload["last_applied_funding_time"])
                if payload.get("last_applied_funding_time")
                else None
            ),
        )

    def clear(self, venue: Venue, symbol: str) -> None:
        path = self._path(venue, symbol)
        path.unlink(missing_ok=True)
