"""Every OrderIntent passes through here before it reaches the executor —
there is no path to the exchange that bypasses this module (see the
architecture diagram: the risk-module arrow is one-directional, a rejected
intent goes only to the log). The checks here are safety fuses, not
strategy parameters: they don't decide whether a trade is a good idea, only
whether it's allowed to happen at all.
"""

from __future__ import annotations

import structlog
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from fundarb.config import RebalanceConfig, RiskConfig
from fundarb.core.models import OrderIntent
from fundarb.core.types import IntentReason, OrderSide, RebalanceMode, RiskDecision

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RiskCheckResult:
    decision: RiskDecision
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.decision is RiskDecision.APPROVED


@dataclass(frozen=True)
class RebalanceDecision:
    should_rebalance: bool
    side: OrderSide | None = None
    notional: Decimal = Decimal(0)
    deviation_pct: Decimal = Decimal(0)
    should_alert: bool = False
    reason: str = ""


class RiskGuard:
    def __init__(self, risk_cfg: RiskConfig, rebalance_cfg: RebalanceConfig) -> None:
        self.risk_cfg = risk_cfg
        self.rebalance_cfg = rebalance_cfg
        self._order_timestamps: deque[datetime] = deque()
        self._daily_realized_pnl = Decimal(0)
        self._daily_date = None
        self._kill_switch_active = False
        self._kill_switch_reason = ""
        self._last_rebalance_at: datetime | None = None

    # ---- kill switch ------------------------------------------------

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    def trigger_kill_switch(self, reason: str) -> None:
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        log.error("kill switch triggered", reason=reason)

    def reset_kill_switch(self) -> None:
        """Manual reset only — never called automatically. The spec is
        explicit: the kill switch blocks new entries until a human clears
        it, closing both legs is not itself grounds for resuming.
        """
        self._kill_switch_active = False
        self._kill_switch_reason = ""
        log.warning("kill switch manually reset")

    # ---- intent gate --------------------------------------------------

    def check(
        self,
        intent: OrderIntent,
        *,
        price: Decimal,
        current_position_notional_usd: Decimal,
        total_exposure_usd: Decimal,
        now: datetime | None = None,
    ) -> RiskCheckResult:
        now = now or datetime.now(timezone.utc)

        if self._kill_switch_active and intent.reason is not IntentReason.KILL_SWITCH_CLOSE:
            return self._reject(f"kill switch active: {self._kill_switch_reason}")

        self._prune_order_timestamps(now)
        if len(self._order_timestamps) >= self.risk_cfg.max_orders_per_minute:
            return self._reject(
                f"order frequency limit exceeded ({self.risk_cfg.max_orders_per_minute}/min)"
            )

        notional = intent.quantity * price

        if not intent.reduce_only:
            new_position_notional = current_position_notional_usd + notional
            if new_position_notional > self.risk_cfg.max_position_notional_usd:
                return self._reject(
                    f"position notional {new_position_notional} would exceed "
                    f"max_position_notional_usd={self.risk_cfg.max_position_notional_usd}"
                )
            new_total_exposure = total_exposure_usd + notional
            if new_total_exposure > self.risk_cfg.max_total_exposure_usd:
                return self._reject(
                    f"total exposure {new_total_exposure} would exceed "
                    f"max_total_exposure_usd={self.risk_cfg.max_total_exposure_usd}"
                )

        self._order_timestamps.append(now)
        return RiskCheckResult(decision=RiskDecision.APPROVED)

    @staticmethod
    def _reject(reason: str) -> RiskCheckResult:
        log.warning("intent rejected by risk guard", reason=reason)
        return RiskCheckResult(decision=RiskDecision.REJECTED, reason=reason)

    def _prune_order_timestamps(self, now: datetime) -> None:
        cutoff = now.timestamp() - 60
        while self._order_timestamps and self._order_timestamps[0].timestamp() < cutoff:
            self._order_timestamps.popleft()

    # ---- leverage -------------------------------------------------------

    def check_leverage(self, notional: Decimal, margin: Decimal) -> RiskCheckResult:
        if margin <= 0:
            return self._reject("perp margin is zero or negative")
        implied_leverage = notional / margin
        if implied_leverage > self.risk_cfg.max_leverage:
            return self._reject(
                f"implied leverage {implied_leverage:.2f}x exceeds max_leverage={self.risk_cfg.max_leverage}x"
            )
        return RiskCheckResult(decision=RiskDecision.APPROVED)

    # ---- delta rebalance (auto, per config decision) -------------------

    def check_delta(
        self,
        *,
        spot_notional: Decimal,
        perp_notional: Decimal,
        now: datetime | None = None,
    ) -> RebalanceDecision:
        """Delta = spot notional - perp notional (both positive magnitudes,
        spot long / perp short). A nonzero delta means the position has
        stopped being market-neutral.
        """
        now = now or datetime.now(timezone.utc)
        position_notional = max(spot_notional, perp_notional)
        if position_notional == 0:
            return RebalanceDecision(should_rebalance=False)

        delta = spot_notional - perp_notional
        deviation_pct = abs(delta) / position_notional * 100
        if deviation_pct <= self.rebalance_cfg.max_delta_deviation_pct:
            return RebalanceDecision(should_rebalance=False, deviation_pct=deviation_pct)

        reason = (
            f"delta deviation {deviation_pct:.2f}% exceeds "
            f"max_delta_deviation_pct={self.rebalance_cfg.max_delta_deviation_pct}%"
        )

        if self.rebalance_cfg.mode is RebalanceMode.ALERT_ONLY:
            return RebalanceDecision(
                should_rebalance=False, deviation_pct=deviation_pct, should_alert=True, reason=reason
            )

        if (
            self._last_rebalance_at is not None
            and (now - self._last_rebalance_at).total_seconds() < self.rebalance_cfg.min_rebalance_interval_sec
        ):
            return RebalanceDecision(
                should_rebalance=False,
                deviation_pct=deviation_pct,
                should_alert=True,
                reason=f"{reason}; rebalance suppressed by cooldown",
            )

        cap = position_notional * self.rebalance_cfg.max_rebalance_notional_pct / 100
        rebalance_notional = min(abs(delta), cap)
        # spot exceeds perp -> spot leg is too big -> sell spot to shrink it (and vice versa)
        side = OrderSide.SELL if delta > 0 else OrderSide.BUY
        return RebalanceDecision(
            should_rebalance=True,
            side=side,
            notional=rebalance_notional,
            deviation_pct=deviation_pct,
            reason=reason,
        )

    def record_rebalance(self, now: datetime | None = None) -> None:
        self._last_rebalance_at = now or datetime.now(timezone.utc)

    # ---- daily loss limit -> kill switch --------------------------------

    def record_realized_pnl(self, pnl_delta: Decimal, now: datetime | None = None) -> bool:
        """Returns True if this update triggered the kill switch."""
        now = now or datetime.now(timezone.utc)
        today = now.date()
        if self._daily_date != today:
            self._daily_date = today
            self._daily_realized_pnl = Decimal(0)
        self._daily_realized_pnl += pnl_delta
        if self._daily_realized_pnl <= -self.risk_cfg.daily_loss_limit_usd:
            self.trigger_kill_switch(
                f"daily loss {self._daily_realized_pnl} breached "
                f"daily_loss_limit_usd={self.risk_cfg.daily_loss_limit_usd}"
            )
            return True
        return False
