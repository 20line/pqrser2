"""The most consequential module in the system: turns a risk-approved pair
of intents into a filled delta-neutral position, or into nothing at all.
There is no state in between that this code will leave standing — a leg
that can't be matched by its partner gets rolled back immediately rather
than left as a silent directional bet.

Sequence for entry (mirrors exit symmetrically, reduce_only=True on both
legs, cumulative_funding_pnl already realized so no rollback ambiguity):
  1. size both legs from current quotes
  2. place leg 1, wait for fill with timeout
  3. place leg 2 sized to leg 1's *actual* filled quantity, wait with timeout
  4. leg 2 failure -> immediately unwind leg 1 at market
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

import structlog

from fundarb.core.errors import ExchangeAdapterError, OrderTimeoutError
from fundarb.core.models import OrderAck, OrderIntent
from fundarb.core.types import IntentReason, Market, OrderSide, OrderStatus, OrderType, Venue
from fundarb.exchanges.base import ExchangeAdapter
from fundarb.risk.guard import RiskGuard

log = structlog.get_logger(__name__)


@dataclass
class LegResult:
    intent: OrderIntent
    ack: OrderAck


@dataclass
class TwoLegResult:
    success: bool
    leg1: LegResult | None = None
    leg2: LegResult | None = None
    rolled_back: bool = False
    reason: str = ""


def deterministic_client_order_id(
    venue: Venue, symbol: str, reason: IntentReason, epoch_key: str, leg: int
) -> str:
    """Built from (venue, symbol, reason, epoch_key, leg) only — resending
    the same logical action after a disconnect reproduces the same id
    instead of creating a duplicate order. `epoch_key` is caller-supplied
    (e.g. a scan cycle id or position id) so genuinely new actions still get
    fresh ids.
    """
    raw = f"{venue.value}:{symbol}:{reason.value}:{epoch_key}:{leg}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"fa-{digest}"


class TwoLegExecutor:
    def __init__(self, adapter: ExchangeAdapter, risk: RiskGuard, *, leg_fill_timeout_sec: float) -> None:
        self.adapter = adapter
        self.risk = risk
        self.leg_fill_timeout_sec = leg_fill_timeout_sec

    async def execute(
        self,
        *,
        symbol: str,
        leg1_market: Market,
        leg1_side: OrderSide,
        leg2_market: Market,
        leg2_side: OrderSide,
        quantity: Decimal,
        price_leg1: Decimal,
        price_leg2: Decimal,
        reason: IntentReason,
        epoch_key: str,
        current_position_notional_usd: Decimal,
        total_exposure_usd: Decimal,
        reduce_only: bool = False,
    ) -> TwoLegResult:
        leg1_intent = OrderIntent(
            venue=self.adapter.venue,
            symbol=symbol,
            market=leg1_market,
            side=leg1_side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            reduce_only=reduce_only,
            client_order_id=deterministic_client_order_id(
                self.adapter.venue, symbol, reason, epoch_key, 1
            ),
            reason=reason,
        )
        check1 = self.risk.check(
            leg1_intent,
            price=price_leg1,
            current_position_notional_usd=current_position_notional_usd,
            total_exposure_usd=total_exposure_usd,
        )
        if not check1.approved:
            return TwoLegResult(success=False, reason=f"leg1 rejected by risk guard: {check1.reason}")

        try:
            ack1 = await self.adapter.place_order(leg1_intent)
            filled1 = await self._await_fill(leg1_intent, ack1)
        except (ExchangeAdapterError, OrderTimeoutError) as exc:
            log.error("leg1 failed, nothing to roll back", symbol=symbol, error=str(exc))
            return TwoLegResult(success=False, reason=f"leg1 failed: {exc}")

        leg1_result = LegResult(intent=leg1_intent, ack=filled1)
        if filled1.filled_quantity <= 0:
            return TwoLegResult(success=False, leg1=leg1_result, reason="leg1 filled zero quantity")

        leg2_intent = OrderIntent(
            venue=self.adapter.venue,
            symbol=symbol,
            market=leg2_market,
            side=leg2_side,
            quantity=filled1.filled_quantity,  # sized to leg1's ACTUAL fill, not the request
            order_type=OrderType.MARKET,
            reduce_only=reduce_only,
            client_order_id=deterministic_client_order_id(
                self.adapter.venue, symbol, reason, epoch_key, 2
            ),
            reason=reason,
        )
        check2 = self.risk.check(
            leg2_intent,
            price=price_leg2,
            current_position_notional_usd=current_position_notional_usd,
            total_exposure_usd=total_exposure_usd,
        )
        if not check2.approved:
            rolled_back = await self._rollback(leg1_intent, filled1, epoch_key)
            return TwoLegResult(
                success=False,
                leg1=leg1_result,
                rolled_back=rolled_back,
                reason=f"leg2 rejected by risk guard: {check2.reason}",
            )

        try:
            ack2 = await self.adapter.place_order(leg2_intent)
            filled2 = await self._await_fill(leg2_intent, ack2)
        except (ExchangeAdapterError, OrderTimeoutError) as exc:
            log.error("leg2 failed, rolling back leg1", symbol=symbol, error=str(exc))
            rolled_back = await self._rollback(leg1_intent, filled1, epoch_key)
            return TwoLegResult(
                success=False,
                leg1=leg1_result,
                rolled_back=rolled_back,
                reason=f"leg2 failed: {exc}",
            )

        return TwoLegResult(
            success=True,
            leg1=leg1_result,
            leg2=LegResult(intent=leg2_intent, ack=filled2),
        )

    async def execute_single(
        self,
        *,
        symbol: str,
        market: Market,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        reason: IntentReason,
        epoch_key: str,
        current_position_notional_usd: Decimal,
        total_exposure_usd: Decimal,
        reduce_only: bool = True,
    ) -> LegResult | None:
        """One order, no partner leg — used for delta rebalancing, where the
        whole point is to trim a single side back toward the other. Returns
        None (and logs) on rejection or failure rather than raising, since a
        rebalance miss this cycle is retried next cycle, not fatal.
        """
        intent = OrderIntent(
            venue=self.adapter.venue,
            symbol=symbol,
            market=market,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            reduce_only=reduce_only,
            client_order_id=deterministic_client_order_id(self.adapter.venue, symbol, reason, epoch_key, 1),
            reason=reason,
        )
        check = self.risk.check(
            intent,
            price=price,
            current_position_notional_usd=current_position_notional_usd,
            total_exposure_usd=total_exposure_usd,
        )
        if not check.approved:
            log.warning("rebalance leg rejected by risk guard", symbol=symbol, reason=check.reason)
            return None
        try:
            ack = await self.adapter.place_order(intent)
            filled = await self._await_fill(intent, ack)
            return LegResult(intent=intent, ack=filled)
        except (ExchangeAdapterError, OrderTimeoutError) as exc:
            log.error("rebalance leg failed", symbol=symbol, error=str(exc))
            return None

    async def _await_fill(self, intent: OrderIntent, ack: OrderAck) -> OrderAck:
        if ack.status is OrderStatus.FILLED:
            return ack
        return await self.adapter.wait_for_fill(
            ack.venue_order_id, intent.symbol, intent.market, self.leg_fill_timeout_sec
        )

    async def _rollback(self, filled_intent: OrderIntent, ack: OrderAck, epoch_key: str) -> bool:
        """Immediately close out the quantity that DID fill on leg 1, at
        market, opposite side. This is a best-effort unwind: if it also
        fails, the position is genuinely directional and reconciliation +
        an alert are what's left — see monitor.alerts.
        """
        opposite_side = OrderSide.SELL if filled_intent.side is OrderSide.BUY else OrderSide.BUY
        rollback_intent = OrderIntent(
            venue=self.adapter.venue,
            symbol=filled_intent.symbol,
            market=filled_intent.market,
            side=opposite_side,
            quantity=ack.filled_quantity,
            order_type=OrderType.MARKET,
            reduce_only=True,
            client_order_id=deterministic_client_order_id(
                self.adapter.venue, filled_intent.symbol, IntentReason.ENTRY_ROLLBACK, epoch_key, 1
            ),
            reason=IntentReason.ENTRY_ROLLBACK,
        )
        try:
            rb_ack = await self.adapter.place_order(rollback_intent)
            await self._await_fill(rollback_intent, rb_ack)
            log.warning("leg1 rolled back", symbol=filled_intent.symbol, quantity=str(ack.filled_quantity))
            return True
        except (ExchangeAdapterError, OrderTimeoutError) as exc:
            log.critical(
                "ROLLBACK FAILED — position is directional, manual intervention required",
                symbol=filled_intent.symbol,
                error=str(exc),
            )
            self.risk.trigger_kill_switch(f"rollback failed for {filled_intent.symbol}: {exc}")
            return False
