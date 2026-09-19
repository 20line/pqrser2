"""Local state after a restart is always untrusted. On start, this pulls
the real positions and open orders from the exchange and treats them as
ground truth — the alternative (trusting whatever the process last wrote to
disk before it died) is exactly how a restart during position-opening turns
into a silent unhedged leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

from fundarb.core.errors import ReconciliationError
from fundarb.core.models import Order
from fundarb.core.types import Market, OrderSide
from fundarb.exchanges.base import ExchangeAdapter

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SymbolState:
    symbol: str
    spot_quantity: Decimal
    perp_quantity: Decimal
    open_orders: list[Order]

    @property
    def delta_notional_ratio(self) -> Decimal:
        """abs(spot - perp) / max(spot, perp) as a fraction; 0 = perfectly
        hedged, 1 = fully one-legged.
        """
        larger = max(self.spot_quantity, self.perp_quantity)
        if larger == 0:
            return Decimal(0)
        return abs(self.spot_quantity - self.perp_quantity) / larger

    @property
    def is_single_legged(self) -> bool:
        return (self.spot_quantity > 0) != (self.perp_quantity > 0)


async def reconcile(adapter: ExchangeAdapter) -> dict[str, SymbolState]:
    """Queries the exchange for real positions/orders and builds a per-symbol
    view. Raises ReconciliationError if the exchange calls themselves fail —
    starting with an unknown state is worse than not starting at all.
    """
    try:
        positions = await adapter.get_positions()
        open_orders = await adapter.get_open_orders()
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal to startup
        raise ReconciliationError(f"{adapter.venue}: reconciliation failed: {exc}") from exc

    by_symbol: dict[str, SymbolState] = {}
    orders_by_symbol: dict[str, list[Order]] = {}
    for o in open_orders:
        orders_by_symbol.setdefault(o.symbol, []).append(o)

    symbols = {p.symbol for p in positions} | set(orders_by_symbol)
    for symbol in symbols:
        spot_qty = Decimal(0)
        perp_qty = Decimal(0)
        for p in positions:
            if p.symbol != symbol:
                continue
            signed = p.quantity if p.side is OrderSide.BUY else -p.quantity
            if p.market is Market.SPOT:
                spot_qty += signed
            else:
                perp_qty += signed
        state = SymbolState(
            symbol=symbol,
            spot_quantity=spot_qty,
            perp_quantity=perp_qty,
            open_orders=orders_by_symbol.get(symbol, []),
        )
        by_symbol[symbol] = state
        if state.is_single_legged:
            log.critical(
                "RECONCILIATION FOUND A SINGLE-LEGGED POSITION",
                symbol=symbol,
                spot_quantity=str(spot_qty),
                perp_quantity=str(perp_qty),
            )

    log.info("reconciliation complete", venue=adapter.venue, symbols=len(by_symbol))
    return by_symbol
