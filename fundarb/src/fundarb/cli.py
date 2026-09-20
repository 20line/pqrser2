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

import click
import structlog

from fundarb.backtest.engine import BacktestEngine
from fundarb.backtest.strategy import CarryStrategy
from fundarb.collect.history import HistoryCollector
from fundarb.collect.storage import ParquetStorage
from fundarb.config import FundarbConfig, Secrets, load_config, load_secrets
from fundarb.core.types import Venue
from fundarb.exchanges.base import ExchangeAdapter
from fundarb.exchanges.binance import BinanceAdapter
from fundarb.exchanges.bybit import BybitAdapter
from fundarb.execution.executor import TwoLegExecutor
from fundarb.execution.journal import PositionJournal
from fundarb.execution.ledger import TradeLedger
from fundarb.execution.live_runner import LiveRunner
from fundarb.monitor.alerts import TelegramAlerter
from fundarb.monitor.metrics import MetricsRegistry
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
    strategy = CarryStrategy(
        config.entry, config.exit_rule, config.risk.max_position_notional_usd, config.universe.max_spread_bps
    )
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
    executor = TwoLegExecutor(adapter, risk, leg_fill_timeout_sec=config.risk.leg_fill_timeout_sec)
    strategy = CarryStrategy(
        config.entry, config.exit_rule, config.risk.max_position_notional_usd, config.universe.max_spread_bps
    )

    runner = LiveRunner(
        venue=venue,
        symbol=symbol,
        config=config,
        adapter=adapter,
        storage=storage,
        risk=risk,
        executor=executor,
        strategy=strategy,
        alerter=alerter,
        metrics=MetricsRegistry(),
        journal=PositionJournal(data_dir),
        ledger=TradeLedger(data_dir),
    )
    try:
        await runner.start()
        while True:
            # run_once() is documented not to raise — it's its own backstop
            # against unexpected exceptions, so this loop stays deliberately
            # bare rather than duplicating that handling here.
            await runner.run_once()
            await asyncio.sleep(poll_interval)
    finally:
        await runner.close()


if __name__ == "__main__":
    main()
