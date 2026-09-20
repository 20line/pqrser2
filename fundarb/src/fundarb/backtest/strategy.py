"""Strategy interface shared by the backtest engine and the live run loop —
the same CarryStrategy instance drives both, so a strategy that passed
backtest is byte-for-byte the strategy that trades. It never sees an
exchange: it only turns state (funding events, quotes, open position) into
decisions, which OrderIntents downstream of it get risk-checked before
anything is sent to an adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fundarb.config import EntryConfig, ExitRuleConfig
from fundarb.core.types import ExitRuleMode, Venue
from fundarb.research.yield_calc import basis_pnl


@dataclass
class OpenPosition:
    venue: Venue
    symbol: str
    entry_time: datetime
    entry_basis: Decimal
    notional: Decimal
    entry_rate_apr_pct: Decimal
    cumulative_funding_pnl: Decimal = Decimal(0)
    consecutive_negative_periods: int = 0
    periods_held: int = 0
    # Live-run only: actual filled leg sizes, used for delta-rebalance checks.
    # The backtest engine never touches these — it assumes perfect hedges.
    spot_qty: Decimal = Decimal(0)
    perp_qty: Decimal = Decimal(0)
    # Live-run only: high-water mark of the last funding event already
    # folded into cumulative_funding_pnl. Persisted (see execution/journal.py)
    # so a restart doesn't replay — and double-count — events already applied.
    last_applied_funding_time: datetime | None = None


@dataclass
class EntryDecision:
    should_enter: bool
    notional: Decimal = Decimal(0)
    reason: str = ""


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str = ""


class Strategy(ABC):
    @abstractmethod
    def decide_entry(self, *, net_apr_estimate_pct: Decimal, basis_bps: Decimal) -> EntryDecision: ...

    @abstractmethod
    def on_funding_event(self, position: OpenPosition, rate: Decimal) -> None:
        """Update position state (cumulative funding, streak counters) for
        one funding accrual. Mutates `position` in place.
        """

    @abstractmethod
    def decide_exit(
        self, position: OpenPosition, *, current_basis: Decimal, latest_rate: Decimal
    ) -> ExitDecision: ...


class CarryStrategy(Strategy):
    """The strategy the spec describes: enter when net APR clears the
    configured threshold, exit by whichever rule the deployment picked.

      * fixed_profit — mirrors the Hummingbot funding-arb script: sum
        received funding payments plus the basis-driven mark PnL of both
        legs, close once that sum clears `target_pct_of_notional`.
      * rate_reversal — hold through the strategy's edge as long as it
        holds; exit once the rate has been negative for
        `consecutive_negative_periods` in a row (a durable reversal, not
        one noisy print).
    """

    def __init__(
        self,
        entry_cfg: EntryConfig,
        exit_cfg: ExitRuleConfig,
        position_notional: Decimal,
        max_spread_bps: Decimal | None = None,
    ) -> None:
        self.entry_cfg = entry_cfg
        self.exit_cfg = exit_cfg
        self.position_notional = position_notional
        # None disables the check (used by tests that don't care about it);
        # real deployments pass config.universe.max_spread_bps.
        self.max_spread_bps = max_spread_bps

    def decide_entry(self, *, net_apr_estimate_pct: Decimal, basis_bps: Decimal) -> EntryDecision:
        """Basis width is checked here, not only in the scanner: the
        scanner's view can be stale by the time an entry actually fires, and
        the spec's own risk table lists "basis accounted for in the entry
        condition" as the mitigation for basis-convergence risk.
        """
        if self.max_spread_bps is not None and abs(basis_bps) > self.max_spread_bps:
            return EntryDecision(
                should_enter=False,
                reason=f"basis {basis_bps:.1f}bps exceeds max_spread_bps={self.max_spread_bps}",
            )
        if net_apr_estimate_pct < self.entry_cfg.min_net_apr_pct:
            return EntryDecision(
                should_enter=False,
                reason=(
                    f"net APR {net_apr_estimate_pct:.2f}% below threshold "
                    f"{self.entry_cfg.min_net_apr_pct}%"
                ),
            )
        return EntryDecision(should_enter=True, notional=self.position_notional, reason="entry threshold cleared")

    def on_funding_event(self, position: OpenPosition, rate: Decimal) -> None:
        position.cumulative_funding_pnl += rate * position.notional
        position.periods_held += 1
        if rate <= self.exit_cfg.rate_reversal.min_negative_rate_pct / 100:
            position.consecutive_negative_periods += 1
        else:
            position.consecutive_negative_periods = 0

    def decide_exit(
        self, position: OpenPosition, *, current_basis: Decimal, latest_rate: Decimal
    ) -> ExitDecision:
        if self.exit_cfg.mode is ExitRuleMode.FIXED_PROFIT:
            return self._decide_exit_fixed_profit(position, current_basis)
        return self._decide_exit_rate_reversal(position)

    def _decide_exit_fixed_profit(self, position: OpenPosition, current_basis: Decimal) -> ExitDecision:
        total_pnl = position.cumulative_funding_pnl + basis_pnl(
            position.entry_basis, current_basis, position.notional
        )
        target = self.exit_cfg.fixed_profit.target_pct_of_notional / 100 * position.notional
        if total_pnl >= target:
            return ExitDecision(
                should_exit=True,
                reason=f"accrued PnL {total_pnl:.4f} reached target {target:.4f}",
            )
        return ExitDecision(should_exit=False)

    def _decide_exit_rate_reversal(self, position: OpenPosition) -> ExitDecision:
        needed = self.exit_cfg.rate_reversal.consecutive_negative_periods
        if position.consecutive_negative_periods >= needed:
            return ExitDecision(
                should_exit=True,
                reason=f"rate negative for {position.consecutive_negative_periods} consecutive periods",
            )
        return ExitDecision(should_exit=False)
