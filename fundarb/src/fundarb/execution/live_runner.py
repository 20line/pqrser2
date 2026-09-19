"""Drives one symbol on one venue through repeated decision cycles. Split
out of `cli.py` so each fix below is independently testable against a fake
adapter instead of only exercisable by actually running the CLI:

  1. **Funding actually gets collected while trading.** Each cycle backfills
     a short recent window before making any decision — without this, the
     strategy's `cumulative_funding_pnl` never updates unless a *separate*
     `collect` process happens to be running against the same data
     directory, which nothing enforced or even checked.
  2. **The kill switch actually closes positions.** Previously it only
     blocked new entries; an open position stayed open indefinitely. Now
     kill-switch-active is checked first each cycle, and an open position
     is closed immediately (bypassing the normal risk gate, same as the
     executor's own leg-1 rollback does) before anything else runs.
  3. **Leverage is set and checked, not assumed.** `set_leverage` is called
     before the first entry, and `RiskGuard.check_leverage` is checked
     against the account's real perp margin balance — the account default
     leverage is not trusted.
  4. **Connection loss is tracked and alerted**, not just modeled in
     `monitor/metrics.py` with nothing calling it.

Position metadata (entry_time, entry_basis, funding/streak counters) is
persisted via `execution/journal.py` after every cycle that touches it, so
a restart recovers exact state instead of approximating "now".

On top of that: a position-level stop-loss (`risk.max_unrealized_loss_usd`)
forces a close independent of whatever the strategy's own exit rule says;
`MetricsRegistry` is actually populated every cycle (accumulated funding,
basis, delta, margin) instead of sitting unused; `margin_low` and
`rate_reversal` alerts fire per the spec's monitor requirements; and every
closed position is appended to `execution/ledger.py`'s trade ledger —
the "full journal from day one" the spec requires for tax reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog

from fundarb.backtest.strategy import CarryStrategy, OpenPosition
from fundarb.collect.history import HistoryCollector
from fundarb.collect.storage import ParquetStorage
from fundarb.config import FundarbConfig
from fundarb.core.errors import ExchangeAdapterError
from fundarb.core.types import IntentReason, Market, OrderSide, RebalanceMode, Venue
from fundarb.exchanges.base import ExchangeAdapter
from fundarb.exchanges.symbols import split
from fundarb.execution.executor import TwoLegExecutor
from fundarb.execution.journal import PositionJournal
from fundarb.execution.ledger import LedgerEntry, TradeLedger
from fundarb.execution.reconcile import reconcile
from fundarb.monitor.alerts import TelegramAlerter
from fundarb.monitor.metrics import MetricsRegistry
from fundarb.research.fees import round_trip_cost_fraction
from fundarb.research.yield_calc import annualized_raw_rate, basis_fraction
from fundarb.risk.guard import RiskGuard

log = structlog.get_logger(__name__)

_ASSUMED_HOLDING_DAYS = 3
_FUNDING_LOOKBACK_FOR_STRATEGY = 90
_COLLECT_LOOKBACK_DAYS = 2


@dataclass
class LiveRunner:
    venue: Venue
    symbol: str
    config: FundarbConfig
    adapter: ExchangeAdapter
    storage: ParquetStorage
    risk: RiskGuard
    executor: TwoLegExecutor
    strategy: CarryStrategy
    alerter: TelegramAlerter
    metrics: MetricsRegistry
    journal: PositionJournal
    ledger: TradeLedger

    def __post_init__(self) -> None:
        self._collector = HistoryCollector(self.adapter, self.storage)
        self.position: OpenPosition | None = None
        self._leverage_set = False

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Reconciles real exchange state and recovers persisted position
        metadata. Must be called once before the first `run_once()`.
        """
        states = await reconcile(self.adapter, symbols=[self.symbol])
        existing = states.get(self.symbol)

        if existing is None or (existing.spot_quantity == 0 and existing.perp_quantity == 0):
            self.journal.clear(self.venue, self.symbol)
            self.position = None
            return

        if existing.is_single_legged:
            await self.alerter.send(
                f"🚨 {self.symbol}: reconciliation found a single-legged position "
                f"(spot={existing.spot_quantity}, perp={existing.perp_quantity}) — needs manual review"
            )
            self.risk.trigger_kill_switch("single-legged position found at startup")
            # keep whatever the journal has, if anything, so a later manual
            # kill-switch reset has something to close against
            self.position = self.journal.load(self.venue, self.symbol)
            return

        journaled = self.journal.load(self.venue, self.symbol)
        if journaled is not None:
            journaled.spot_qty = existing.spot_quantity
            journaled.perp_qty = abs(existing.perp_quantity)
            self.position = journaled
            log.info("resumed position from journal", symbol=self.symbol)
            return

        log.warning(
            "resuming with an already-open position but no journal entry; "
            "entry_time/entry_basis are unknown and approximated as now",
            symbol=self.symbol,
        )
        spot_quote = await self.adapter.get_quote(self.symbol, Market.SPOT)
        perp_quote = await self.adapter.get_quote(self.symbol, Market.PERP)
        basis = basis_fraction(spot_quote.mid, perp_quote.mid)
        self.position = OpenPosition(
            venue=self.venue,
            symbol=self.symbol,
            entry_time=datetime.now(timezone.utc),
            entry_basis=basis,
            notional=existing.spot_quantity * spot_quote.mid,
            entry_rate_apr_pct=Decimal(0),
            spot_qty=existing.spot_quantity,
            perp_qty=abs(existing.perp_quantity),
        )
        self.journal.save(self.position)

    async def close(self) -> None:
        await self.adapter.close()

    # ---- one decision cycle ---------------------------------------------

    async def run_once(self) -> None:
        epoch_key = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")

        if self.risk.kill_switch_active:
            if self.position is not None:
                await self._emergency_close(epoch_key)
            return

        try:
            spot_quote = await self.adapter.get_quote(self.symbol, Market.SPOT)
            perp_quote = await self.adapter.get_quote(self.symbol, Market.PERP)
        except ExchangeAdapterError as exc:
            await self._handle_disconnect(exc)
            return
        self.metrics.heartbeat(self.venue)

        try:
            lookback_start = datetime.now(timezone.utc) - timedelta(days=_COLLECT_LOOKBACK_DAYS)
            await self._collector.backfill_symbol(self.symbol, lookback_start)
        except ExchangeAdapterError as exc:
            log.warning("live funding/price collection failed this cycle", symbol=self.symbol, error=str(exc))

        basis = basis_fraction(spot_quote.mid, perp_quote.mid)

        if self.position is None:
            await self._try_enter(spot_quote, perp_quote, basis, epoch_key)
        else:
            await self._apply_funding_events()
            await self._monitor_position(basis, spot_quote, perp_quote)
            stopped_out = await self._check_stop_loss(basis, spot_quote, perp_quote, epoch_key)
            if not stopped_out:
                await self._maybe_rebalance(spot_quote, perp_quote, epoch_key)
                if self.position is not None:  # rebalance never closes the position
                    await self._maybe_exit(basis, spot_quote, perp_quote, epoch_key)

        if self.position is not None:
            self.journal.save(self.position)

    # ---- entry ------------------------------------------------------------

    async def _try_enter(self, spot_quote, perp_quote, basis: Decimal, epoch_key: str) -> None:
        rates = self.storage.read_funding_rates_typed(self.venue, self.symbol)[-_FUNDING_LOOKBACK_FOR_STRATEGY:]
        fees_fraction = round_trip_cost_fraction(self.config.fees, self.venue)
        raw_apr = annualized_raw_rate(rates) * 100
        assumed_holding_years = Decimal(_ASSUMED_HOLDING_DAYS) / Decimal(365)
        cost_drag_pct = (fees_fraction / assumed_holding_years) * 100
        net_apr_estimate = raw_apr - cost_drag_pct

        decision = self.strategy.decide_entry(net_apr_estimate_pct=net_apr_estimate, basis_bps=basis * 10_000)
        if not decision.should_enter:
            return

        if not self._leverage_set:
            try:
                await self.adapter.set_leverage(self.symbol, self.config.risk.max_leverage)
                self._leverage_set = True
            except ExchangeAdapterError as exc:
                log.error("failed to set leverage, skipping entry this cycle", symbol=self.symbol, error=str(exc))
                return

        _, quote_asset = split(self.symbol)
        try:
            margin = await self.adapter.get_perp_margin_balance(quote_asset)
        except ExchangeAdapterError as exc:
            log.error("failed to read perp margin balance, skipping entry", symbol=self.symbol, error=str(exc))
            return
        leverage_check = self.risk.check_leverage(decision.notional, margin)
        if not leverage_check.approved:
            log.warning("entry skipped: leverage check failed", symbol=self.symbol, reason=leverage_check.reason)
            return

        qty = (decision.notional / spot_quote.mid).quantize(Decimal("0.0001"))
        result = await self.executor.execute(
            symbol=self.symbol,
            leg1_market=Market.SPOT,
            leg1_side=OrderSide.BUY,
            leg2_market=Market.PERP,
            leg2_side=OrderSide.SELL,
            quantity=qty,
            price_leg1=spot_quote.ask,
            price_leg2=perp_quote.bid,
            reason=IntentReason.ENTRY_LEG1,
            epoch_key=epoch_key,
            current_position_notional_usd=Decimal(0),
            total_exposure_usd=Decimal(0),
        )
        if not result.success:
            log.warning("entry failed", symbol=self.symbol, reason=result.reason)
            return

        self.position = OpenPosition(
            venue=self.venue,
            symbol=self.symbol,
            entry_time=datetime.now(timezone.utc),
            entry_basis=basis,
            notional=decision.notional,
            entry_rate_apr_pct=raw_apr,
            spot_qty=result.leg1.ack.filled_quantity,
            perp_qty=result.leg2.ack.filled_quantity,
        )
        log.info("entered position", symbol=self.symbol, notional=str(decision.notional))

    # ---- ongoing position: funding, rebalance, exit ------------------------

    async def _apply_funding_events(self) -> None:
        assert self.position is not None
        high_water = self.position.last_applied_funding_time or self.position.entry_time
        rates = self.storage.read_funding_rates_typed(self.venue, self.symbol)
        for r in rates:
            if r.funding_time > high_water:
                self.strategy.on_funding_event(self.position, r.rate)
                self.position.last_applied_funding_time = r.funding_time
                high_water = r.funding_time
                if r.rate < 0:
                    # fires regardless of exit_rule.mode — an operator on
                    # fixed_profit still wants to know the edge is gone even
                    # if the strategy itself won't exit on this alone
                    await self.alerter.rate_reversal(self.symbol, float(r.rate * 100))

    async def _monitor_position(self, basis: Decimal, spot_quote, perp_quote) -> None:
        """Populates MetricsRegistry (previously constructed but never
        written to during a live run) and fires margin_low once free perp
        margin drops below monitor.margin_alert_ratio of the position's
        notional.
        """
        assert self.position is not None
        spot_notional = self.position.spot_qty * spot_quote.mid
        perp_notional = self.position.perp_qty * perp_quote.mid
        larger = max(spot_notional, perp_notional)
        delta_ratio = abs(spot_notional - perp_notional) / larger if larger else Decimal(0)

        _, quote_asset = split(self.symbol)
        try:
            margin = await self.adapter.get_perp_margin_balance(quote_asset)
        except ExchangeAdapterError as exc:
            log.warning("failed to read margin balance for monitoring", symbol=self.symbol, error=str(exc))
            return
        margin_ratio = margin / self.position.notional if self.position.notional else Decimal(0)

        self.metrics.update_position(
            self.venue,
            self.symbol,
            accumulated_funding=self.position.cumulative_funding_pnl,
            current_basis_bps=basis * 10_000,
            delta_notional_ratio=delta_ratio,
            margin_ratio=margin_ratio,
        )
        if margin_ratio < self.config.monitor.margin_alert_ratio:
            await self.alerter.margin_low(self.symbol, float(margin_ratio))

    async def _maybe_rebalance(self, spot_quote, perp_quote, epoch_key: str) -> None:
        assert self.position is not None
        decision = self.risk.check_delta(
            spot_notional=self.position.spot_qty * spot_quote.mid,
            perp_notional=self.position.perp_qty * perp_quote.mid,
        )
        if decision.should_rebalance and self.config.rebalance.mode is RebalanceMode.AUTO:
            leg_qty = (decision.notional / spot_quote.mid).quantize(Decimal("0.0001"))
            leg_result = await self.executor.execute_single(
                symbol=self.symbol,
                market=Market.SPOT,
                side=decision.side,
                quantity=leg_qty,
                price=spot_quote.mid,
                reason=IntentReason.DELTA_REBALANCE,
                epoch_key=epoch_key,
                current_position_notional_usd=self.position.notional,
                total_exposure_usd=self.position.notional,
            )
            if leg_result:
                delta_qty = (
                    leg_result.ack.filled_quantity
                    if decision.side is OrderSide.BUY
                    else -leg_result.ack.filled_quantity
                )
                self.position.spot_qty += delta_qty
                self.risk.record_rebalance()
                await self.alerter.delta_out_of_tolerance(self.symbol, float(decision.deviation_pct), True)
        elif decision.should_alert:
            await self.alerter.delta_out_of_tolerance(self.symbol, float(decision.deviation_pct), False)

    def _pnl_breakdown(self, basis: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Returns (funding_pnl, basis_pnl, total) as of `basis` — used both
        for the stop-loss check (unrealized, position still open) and for
        recording/journaling realized PnL once a close actually fills.
        """
        assert self.position is not None
        funding_pnl = self.position.cumulative_funding_pnl
        basis_pnl = -(basis - self.position.entry_basis) * self.position.notional
        return funding_pnl, basis_pnl, funding_pnl + basis_pnl

    def _unrealized_pnl(self, basis: Decimal) -> Decimal:
        return self._pnl_breakdown(basis)[2]

    async def _execute_close(
        self, spot_quote, perp_quote, basis: Decimal, epoch_key: str, reason: IntentReason
    ) -> bool:
        """Shared by every closing path (normal exit, stop-loss, kill
        switch) so realized PnL is recorded consistently — funding AND
        basis drag, not funding alone — and every close lands in the trade
        ledger, not just structlog output.
        """
        assert self.position is not None
        position = self.position
        result = await self.executor.execute(
            symbol=self.symbol,
            leg1_market=Market.PERP,
            leg1_side=OrderSide.BUY,
            leg2_market=Market.SPOT,
            leg2_side=OrderSide.SELL,
            quantity=position.perp_qty,
            price_leg1=perp_quote.ask,
            price_leg2=spot_quote.bid,
            reason=reason,
            epoch_key=epoch_key,
            current_position_notional_usd=position.notional,
            total_exposure_usd=position.notional,
            reduce_only=True,
        )
        if not result.success:
            log.error("position close failed", symbol=self.symbol, reason=reason.value, error=result.reason)
            return False

        funding_pnl, basis_pnl, realized_pnl = self._pnl_breakdown(basis)
        self.risk.record_realized_pnl(realized_pnl)
        exit_time = datetime.now(timezone.utc)
        self.ledger.append(
            LedgerEntry(
                venue=self.venue,
                symbol=self.symbol,
                entry_time=position.entry_time,
                exit_time=exit_time,
                entry_basis=position.entry_basis,
                exit_basis=basis,
                notional=position.notional,
                funding_pnl=funding_pnl,
                basis_pnl=basis_pnl,
                realized_pnl=realized_pnl,
                exit_reason=reason.value,
            )
        )
        log.info("position closed", symbol=self.symbol, reason=reason.value, realized_pnl=str(realized_pnl))
        self.journal.clear(self.venue, self.symbol)
        self.position = None
        return True

    async def _maybe_exit(self, basis: Decimal, spot_quote, perp_quote, epoch_key: str) -> None:
        assert self.position is not None
        decision = self.strategy.decide_exit(self.position, current_basis=basis, latest_rate=Decimal(0))
        if not decision.should_exit:
            return
        await self._execute_close(spot_quote, perp_quote, basis, epoch_key, IntentReason.EXIT_LEG1)

    # ---- stop-loss ----------------------------------------------------

    async def _check_stop_loss(self, basis: Decimal, spot_quote, perp_quote, epoch_key: str) -> bool:
        """Position-level stop-loss on unrealized PnL, independent of
        whatever the strategy's exit rule says. Returns True if a
        stop-loss close was executed (caller skips the normal rebalance
        and exit logic that cycle).
        """
        assert self.position is not None
        unrealized_pnl = self._unrealized_pnl(basis)
        if unrealized_pnl > -self.config.risk.max_unrealized_loss_usd:
            return False

        log.error(
            "stop-loss triggered",
            symbol=self.symbol,
            unrealized_pnl=str(unrealized_pnl),
            max_unrealized_loss_usd=str(self.config.risk.max_unrealized_loss_usd),
        )
        await self.alerter.send(
            f"🚨 {self.symbol}: stop-loss triggered, unrealized PnL {unrealized_pnl:.2f} — closing position"
        )
        closed = await self._execute_close(spot_quote, perp_quote, basis, epoch_key, IntentReason.STOP_LOSS_CLOSE)
        if not closed:
            await self.alerter.send(f"🚨 {self.symbol}: stop-loss close FAILED — will retry next cycle")
        return closed

    # ---- kill switch ------------------------------------------------------

    async def _emergency_close(self, epoch_key: str) -> None:
        assert self.position is not None
        try:
            spot_quote = await self.adapter.get_quote(self.symbol, Market.SPOT)
            perp_quote = await self.adapter.get_quote(self.symbol, Market.PERP)
        except ExchangeAdapterError as exc:
            log.critical(
                "cannot fetch quotes to execute kill-switch close — will retry next cycle",
                symbol=self.symbol,
                error=str(exc),
            )
            return

        basis = basis_fraction(spot_quote.mid, perp_quote.mid)
        closed = await self._execute_close(spot_quote, perp_quote, basis, epoch_key, IntentReason.KILL_SWITCH_CLOSE)
        if closed:
            log.warning("kill-switch close succeeded", symbol=self.symbol)
            await self.alerter.send(f"🛑 {self.symbol}: kill-switch close succeeded, both legs closed")
        else:
            log.critical(
                "kill-switch close FAILED — position may still be open, manual intervention required",
                symbol=self.symbol,
            )
            await self.alerter.send(
                f"🚨 {self.symbol}: kill-switch close FAILED — manual intervention required"
            )

    # ---- connection health --------------------------------------------------

    async def _handle_disconnect(self, exc: ExchangeAdapterError) -> None:
        self.metrics.mark_disconnected(self.venue)
        conn = self.metrics.connections.get(self.venue)
        seconds = conn.seconds_since_heartbeat() if conn else 0.0
        log.error("quote fetch failed", venue=self.venue, symbol=self.symbol, error=str(exc))
        if seconds >= self.config.monitor.connection_loss_alert_sec:
            await self.alerter.connection_lost(self.venue.value, seconds)
