"""In-memory snapshot of what an operator needs to see at a glance: accrued
funding, basis, delta, margin, connection health. Kept intentionally simple
(no metrics backend dependency) — export/scrape wiring is a Phase 5
concern, not a Phase 4 one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from fundarb.core.types import Venue


@dataclass
class PositionMetrics:
    venue: Venue
    symbol: str
    accumulated_funding: Decimal = Decimal(0)
    current_basis_bps: Decimal = Decimal(0)
    delta_notional_ratio: Decimal = Decimal(0)
    margin_ratio: Decimal = Decimal(1)
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ConnectionMetrics:
    venue: Venue
    connected: bool = True
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def seconds_since_heartbeat(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.last_heartbeat).total_seconds()


class MetricsRegistry:
    def __init__(self) -> None:
        self.positions: dict[tuple[Venue, str], PositionMetrics] = {}
        self.connections: dict[Venue, ConnectionMetrics] = {}

    def update_position(
        self,
        venue: Venue,
        symbol: str,
        *,
        accumulated_funding: Decimal,
        current_basis_bps: Decimal,
        delta_notional_ratio: Decimal,
        margin_ratio: Decimal,
    ) -> None:
        self.positions[(venue, symbol)] = PositionMetrics(
            venue=venue,
            symbol=symbol,
            accumulated_funding=accumulated_funding,
            current_basis_bps=current_basis_bps,
            delta_notional_ratio=delta_notional_ratio,
            margin_ratio=margin_ratio,
        )

    def clear_position(self, venue: Venue, symbol: str) -> None:
        """Called on every close — without this the last pre-close snapshot
        (margin ratio, basis, accumulated funding) sits in `positions`
        forever, so anything reading it reports a flat symbol as still
        open and at risk.
        """
        self.positions.pop((venue, symbol), None)

    def heartbeat(self, venue: Venue) -> None:
        self.connections[venue] = ConnectionMetrics(venue=venue, connected=True)

    def mark_disconnected(self, venue: Venue) -> None:
        existing = self.connections.get(venue)
        if existing:
            existing.connected = False
        else:
            self.connections[venue] = ConnectionMetrics(venue=venue, connected=False)
