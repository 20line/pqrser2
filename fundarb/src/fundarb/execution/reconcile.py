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
from fundarb.exchanges.symbols import split

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SymbolState:
    symbol: str
    spot_quantity: Decimal
    perp_quantity: Decimal
    open_orders: list[Order]

    @property
    def delta_notional_ratio(self) -> Decimal:
        """abs(spot - |perp|) / max(spot, |perp|) as a fraction; 0 = perfectly
        hedged, 1 = fully one-legged. `perp_quantity` is signed (negative =
        short, the normal hedge direction), so magnitude is what matters
        here, not sign.
        """
        perp_magnitude = abs(self.perp_quantity)
        larger = max(self.spot_quantity, perp_magnitude)
        if larger == 0:
            return Decimal(0)
        return abs(self.spot_quantity - perp_magnitude) / larger

    @property
    def is_single_legged(self) -> bool:
        """True iff exactly one leg is nonzero. A correctly hedged position
        has spot_quantity > 0 (long) and perp_quantity < 0 (short) — using
        `> 0` on both, as opposed to `!= 0`, would flag every healthy
        position as single-legged.
        """
        return (self.spot_quantity != 0) != (self.perp_quantity != 0)


async def reconcile(adapter: ExchangeAdapter, symbols: list[str] | None = None) -> dict[str, SymbolState]:
    """Queries the exchange for real positions/orders/balances and builds a
    per-symbol view. Raises ReconciliationError if the exchange calls
    themselves fail — starting with an unknown state is worse than not
    starting at all.

    `symbols` should list every symbol the caller trades (a single-symbol
    LiveRunner passes its one symbol). This matters because `get_positions()`
    is a derivatives-only endpoint on every real exchange — spot holdings
    never show up there, they live in `get_balances()`. Without checking
    balances explicitly, every open perp position would look single-legged
    on every restart (spot always reading 0), which is exactly the false
    kill-switch trip this module exists to prevent.
    """
    try:
        positions = await adapter.get_positions()
        open_orders = await adapter.get_open_orders()
        balances = await adapter.get_balances() if symbols else None
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal to startup
        raise ReconciliationError(f"{adapter.venue}: reconciliation failed: {exc}") from exc

    by_symbol: dict[str, SymbolState] = {}
    orders_by_symbol: dict[str, list[Order]] = {}
    for o in open_orders:
        orders_by_symbol.setdefault(o.symbol, []).append(o)

    tracked_symbols = set(symbols or []) | {p.symbol for p in positions} | set(orders_by_symbol)
    for symbol in tracked_symbols:
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
        if balances is not None and symbol in (symbols or []):
            base, _ = split(symbol)
            # spot-account holding of the base asset; perp collateral for a
            # USDT-margined contract lives in the quote asset, not here, so
            # summing get_balances()'s spot+perp totals for `base` is safe
            spot_qty = balances.total.get(base, Decimal(0))
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
