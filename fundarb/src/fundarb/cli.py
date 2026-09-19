"""Command-line entrypoint: collect / scan / backtest / run.

Trading code (the `run` command) is meant to be exercised on testnet first
per the spec's phased rollout — it will refuse to start against mainnet
unless FUNDARB_CONFIRM_MAINNET=1 is set, as a last-line guard against
running it live by accident.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import click
import structlog

from fundarb.backtest.engine import BacktestEngine
from fundarb.backtest.strategy import CarryStrategy, OpenPosition
from fundarb.collect.history import HistoryCollector
from fundarb.collect.storage import ParquetStorage
from fundarb.config import FundarbConfig, Secrets, load_config, load_secrets
from fundarb.core.types import IntentReason, Market, OrderSide, Venue
from fundarb.exchanges.base import ExchangeAdapter
from fundarb.exchanges.binance import BinanceAdapter
from fundarb.exchanges.bybit import BybitAdapter
from fundarb.execution.executor import TwoLegExecutor
from fundarb.execution.reconcile import reconcile
from fundarb.monitor.alerts import TelegramAlerter
from fundarb.monitor.metrics import MetricsRegistry
from fundarb.research.fees import round_trip_cost_fraction
from fundarb.research.yield_calc import annualized_raw_rate, basis_fraction
from fundarb.risk.guard import RiskGuard
from fundarb.scanner.scanner import Scanner

log = structlog.get_logger(__name__)

_ADAPTER_CLASSES = {Venue.BINANCE: BinanceAdapter, Venue.BYBIT: BybitAdapter}


def _build_adapter(venue: Venue, secrets: Secrets, *, testnet: bool) -> ExchangeAdapter:
    cls = _ADAPTER_CLASSES[venue]
    if venue is Venue.BINANCE:
        return cls(api_key=secrets.binance_api_key, api_secret=secrets.binance_api_secret, testnet=testnet)
    return cls(api_key=secrets.bybit_api_key, api_secret=secrets.bybit_api_secret, testnet=testnet)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)
    ctx.obj["secrets"] = load_secrets()


@main.command()
@click.option("--days", default=365, help="Backfill window in days")
@click.option("--data-dir", default="data")
@click.pass_context
def collect(ctx: click.Context, days: int, data_dir: str) -> None:
    """Phase 1: backfill and refresh funding + price history for the universe."""
    config: FundarbConfig = ctx.obj["config"]
    secrets: Secrets = ctx.obj["secrets"]
    asyncio.run(_collect(config, secrets, days, data_dir))


async def _collect(config: FundarbConfig, secrets: Secrets, days: int, data_dir: str) -> None:
    storage = ParquetStorage(data_dir)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    for venue in config.universe.venues:
        adapter = _build_adapter(venue, secrets, testnet=True)
        try:
            instruments = await adapter.fetch_instruments()
            collector = HistoryCollector(adapter, storage)
            await collector.backfill_universe(instruments, start)
        finally:
            await adapter.close()


@main.command()
@click.option("--data-dir", default="data")
@click.option("--top", default=20, help="How many candidates to print")
@click.pass_context
def scan(ctx: click.Context, data_dir: str, top: int) -> None:
    """Phase 2: rank the universe by net APR after costs."""
    config: FundarbConfig = ctx.obj["config"]
    secrets: Secrets = ctx.obj["secrets"]
    asyncio.run(_scan(config, secrets, data_dir, top))


async def _scan(config: FundarbConfig, secrets: Secrets, data_dir: str, top: int) -> None:
    storage = ParquetStorage(data_dir)
    scanner = Scanner(config, storage)
    instruments = []
    for venue in config.universe.venues:
        adapter = _build_adapter(venue, secrets, testnet=True)
        try:
            instruments.extend(await adapter.fetch_instruments())
        finally:
            await adapter.close()

    candidates = scanner.scan(instruments)
    header = f"{'venue':<8} {'symbol':<12} {'raw APR%':>9} {'net APR%':>9} {'neg%':>6} {'status'}"
    click.echo(header)
    for c in candidates[:top]:
        status = "OK" if c.eligible else f"skip: {c.rejected_reason}"
        click.echo(
            f"{c.venue.value:<8} {c.symbol:<12} {c.raw_apr_pct:>9.2f} {c.net_apr_estimate_pct:>9.2f} "
            f"{c.stability.negative_period_share * 100:>6.1f} {status}"
        )


@main.command()
@click.option("--venue", type=click.Choice([v.value for v in Venue]), required=True)
@click.option("--symbol", required=True, help="Normalized symbol, e.g. BTC/USDT")
@click.option("--start", required=True, help="ISO date, e.g. 2025-01-01")
@click.option("--end", default=None, help="ISO date, default now")
@click.option("--data-dir", default="data")
@click.pass_context
def backtest(ctx: click.Context, venue: str, symbol: str, start: str, end: str | None, data_dir: str) -> None:
    """Phase 3: run the carry strategy over stored history."""
    config: FundarbConfig = ctx.obj["config"]
    storage = ParquetStorage(data_dir)
    engine = BacktestEngine(config, storage)
    strategy = CarryStrategy(config.entry, config.exit_rule, config.risk.max_position_notional_usd)
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else datetime.now(timezone.utc)

    result = engine.run(Venue(venue), symbol, start_dt, end_dt, strategy)
    click.echo(f"trades: {len(result.trades)}")
    click.echo(f"total net PnL: {result.total_net_pnl:.2f}")
    click.echo(f"win rate: {result.win_rate:.2%}")
    click.echo(f"avg holding days: {result.avg_holding_days:.2f}")
    for t in result.trades:
        click.echo(
            f"  {t.entry_time.isoformat()} -> {t.exit_time.isoformat()} "
            f"net={t.net_pnl:.2f} ({t.net_pnl_pct_of_notional:.2f}%) exit={t.exit_reason}"
        )


@main.command()
@click.option("--venue", type=click.Choice([v.value for v in Venue]), required=True)
@click.option("--symbol", required=True)
@click.option("--data-dir", default="data")
@click.option("--poll-interval", default=60, help="Seconds between decision cycles")
@click.option("--mainnet", is_flag=True, default=False, help="Trade on mainnet instead of testnet")
@click.pass_context
def run(ctx: click.Context, venue: str, symbol: str, data_dir: str, poll_interval: int, mainnet: bool) -> None:
    """Phase 4/5: live (or testnet) run of a single symbol on one venue.

    Per Phase 5 of the spec: start with minimal size, one pair, and
    reconcile realized PnL against the exchange statement daily before
    expanding.
    """
    if mainnet and not os.environ.get("FUNDARB_CONFIRM_MAINNET"):
        raise click.ClickException(
            "refusing to trade mainnet: set FUNDARB_CONFIRM_MAINNET=1 to confirm you mean it"
        )
    config: FundarbConfig = ctx.obj["config"]
    secrets: Secrets = ctx.obj["secrets"]
    asyncio.run(_run(config, secrets, Venue(venue), symbol, data_dir, poll_interval, testnet=not mainnet))


async def _run(
    config: FundarbConfig,
    secrets: Secrets,
    venue: Venue,
    symbol: str,
    data_dir: str,
    poll_interval: int,
    *,
    testnet: bool,
) -> None:
    storage = ParquetStorage(data_dir)
    adapter = _build_adapter(venue, secrets, testnet=testnet)
    risk = RiskGuard(config.risk, config.rebalance)
    alerter = TelegramAlerter(secrets, enabled=config.monitor.telegram_enabled)
    metrics = MetricsRegistry()
    executor = TwoLegExecutor(adapter, risk, leg_fill_timeout_sec=config.risk.leg_fill_timeout_sec)
    strategy = CarryStrategy(config.entry, config.exit_rule, config.risk.max_position_notional_usd)

    try:
        states = await reconcile(adapter)
        existing = states.get(symbol)
        position: OpenPosition | None = None
        if existing and not existing.is_single_legged and existing.spot_quantity > 0:
            log.warning(
                "resuming with an already-open position; entry_time/entry_basis are unknown "
                "and approximated as now — a real deployment should persist this metadata",
                symbol=symbol,
            )
            spot_quote = await adapter.get_quote(symbol, Market.SPOT)
            perp_quote = await adapter.get_quote(symbol, Market.PERP)
            basis = basis_fraction(spot_quote.mid, perp_quote.mid)
            position = OpenPosition(
                venue=venue,
                symbol=symbol,
                entry_time=datetime.now(timezone.utc),
                entry_basis=basis,
                notional=existing.spot_quantity * spot_quote.mid,
                entry_rate_apr_pct=Decimal(0),
                spot_qty=existing.spot_quantity,
                perp_qty=abs(existing.perp_quantity),
            )
        elif existing and existing.is_single_legged:
            await alerter.send(
                f"🚨 {symbol}: reconciliation found a single-legged position "
                f"(spot={existing.spot_quantity}, perp={existing.perp_quantity}) — needs manual review"
            )
            risk.trigger_kill_switch("single-legged position found at startup")

        while True:
            if risk.kill_switch_active:
                log.error("kill switch active, idling", symbol=symbol)
                await asyncio.sleep(poll_interval)
                continue

            spot_quote = await adapter.get_quote(symbol, Market.SPOT)
            perp_quote = await adapter.get_quote(symbol, Market.PERP)
            basis = basis_fraction(spot_quote.mid, perp_quote.mid)
            epoch_key = datetime.now(timezone.utc).strftime("%Y%m%dT%H")

            if position is None:
                rates = storage.read_funding_rates_typed(venue, symbol)[-90:]
                fees_fraction = round_trip_cost_fraction(config.fees, venue)
                raw_apr = annualized_raw_rate(rates) * 100
                assumed_holding_years = Decimal(3) / Decimal(365)
                cost_drag_pct = (fees_fraction / assumed_holding_years) * 100
                net_apr_estimate = raw_apr - cost_drag_pct
                decision = strategy.decide_entry(net_apr_estimate_pct=net_apr_estimate, basis_bps=basis * 10_000)
                if decision.should_enter:
                    qty = (decision.notional / spot_quote.mid).quantize(Decimal("0.0001"))
                    result = await executor.execute(
                        symbol=symbol,
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
                    if result.success:
                        position = OpenPosition(
                            venue=venue,
                            symbol=symbol,
                            entry_time=datetime.now(timezone.utc),
                            entry_basis=basis,
                            notional=decision.notional,
                            entry_rate_apr_pct=raw_apr,
                            spot_qty=result.leg1.ack.filled_quantity,
                            perp_qty=result.leg2.ack.filled_quantity,
                        )
                        log.info("entered position", symbol=symbol, notional=str(decision.notional))
                    else:
                        log.warning("entry failed", symbol=symbol, reason=result.reason)
            else:
                latest = storage.read_funding_rates_typed(venue, symbol)
                if latest and latest[-1].funding_time > position.entry_time:
                    for r in latest:
                        if r.funding_time > position.entry_time:
                            strategy.on_funding_event(position, r.rate)

                rebalance_decision = risk.check_delta(
                    spot_notional=position.spot_qty * spot_quote.mid,
                    perp_notional=position.perp_qty * perp_quote.mid,
                )
                if rebalance_decision.should_rebalance and config.rebalance.mode.value == "auto":
                    leg_qty = (rebalance_decision.notional / spot_quote.mid).quantize(Decimal("0.0001"))
                    leg_result = await executor.execute_single(
                        symbol=symbol,
                        market=Market.SPOT,
                        side=rebalance_decision.side,
                        quantity=leg_qty,
                        price=spot_quote.mid,
                        reason=IntentReason.DELTA_REBALANCE,
                        epoch_key=epoch_key,
                        current_position_notional_usd=position.notional,
                        total_exposure_usd=position.notional,
                    )
                    if leg_result:
                        delta_qty = (
                            leg_result.ack.filled_quantity
                            if rebalance_decision.side is OrderSide.BUY
                            else -leg_result.ack.filled_quantity
                        )
                        position.spot_qty += delta_qty
                        risk.record_rebalance()
                        await alerter.delta_out_of_tolerance(
                            symbol, float(rebalance_decision.deviation_pct), True
                        )
                elif rebalance_decision.should_alert:
                    await alerter.delta_out_of_tolerance(
                        symbol, float(rebalance_decision.deviation_pct), False
                    )

                exit_decision = strategy.decide_exit(position, current_basis=basis, latest_rate=Decimal(0))
                if exit_decision.should_exit:
                    result = await executor.execute(
                        symbol=symbol,
                        leg1_market=Market.PERP,
                        leg1_side=OrderSide.BUY,
                        leg2_market=Market.SPOT,
                        leg2_side=OrderSide.SELL,
                        quantity=position.perp_qty,
                        price_leg1=perp_quote.ask,
                        price_leg2=spot_quote.bid,
                        reason=IntentReason.EXIT_LEG1,
                        epoch_key=epoch_key,
                        current_position_notional_usd=position.notional,
                        total_exposure_usd=position.notional,
                        reduce_only=True,
                    )
                    if result.success:
                        risk.record_realized_pnl(position.cumulative_funding_pnl)
                        log.info("exited position", symbol=symbol, reason=exit_decision.reason)
                        position = None
                    else:
                        log.error("exit failed", symbol=symbol, reason=result.reason)

            metrics.heartbeat(venue)
            await asyncio.sleep(poll_interval)
    finally:
        await adapter.close()


if __name__ == "__main__":
    main()
