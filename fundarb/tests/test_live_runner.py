from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fundarb.backtest.strategy import CarryStrategy
from fundarb.collect.storage import ParquetStorage
from fundarb.config import ExitRuleConfig, FundarbConfig, RateReversalExit
from fundarb.core.errors import ExchangeAdapterError
from fundarb.core.models import Balances, FundingRate, Position, Quote
from fundarb.core.types import ExitRuleMode, IntentReason, Market, OrderSide, Venue
from fundarb.execution.executor import TwoLegExecutor
from fundarb.execution.journal import PositionJournal
from fundarb.execution.live_runner import LiveRunner
from fundarb.monitor.metrics import MetricsRegistry
from fundarb.risk.guard import RiskGuard
from tests.fakes import FakeAdapter, RecordingAlerter, filled_ack

_SYMBOL = "BTC/USDT"
_VENUE = Venue.BINANCE
_SPOT_PRICE = Decimal("50000")
_PERP_PRICE = Decimal("49990")


def _quotes(spot: Decimal = _SPOT_PRICE, perp: Decimal = _PERP_PRICE) -> dict[Market, Quote]:
    now = datetime.now(timezone.utc)
    return {
        Market.SPOT: Quote(venue=_VENUE, symbol=_SYMBOL, market=Market.SPOT, bid=spot, ask=spot, timestamp=now),
        Market.PERP: Quote(venue=_VENUE, symbol=_SYMBOL, market=Market.PERP, bid=perp, ask=perp, timestamp=now),
    }


def _high_yield_rates(count: int = 10, rate: str = "0.0006") -> list[FundingRate]:
    """Rich enough to clear entry.min_net_apr_pct(15%) after the ~36.5%
    annualized cost drag the default fee schedule implies at the assumed
    3-day holding period.
    """
    now = datetime.now(timezone.utc)
    return [
        FundingRate(
            venue=_VENUE,
            symbol=_SYMBOL,
            funding_time=now - timedelta(hours=8 * (count - i)),
            rate=Decimal(rate),
            interval_hours=8,
            mark_price=_SPOT_PRICE,
        )
        for i in range(count)
    ]


def _build_runner(
    tmp_path,
    fundarb_config: FundarbConfig,
    adapter: FakeAdapter,
    *,
    exit_rule: ExitRuleConfig | None = None,
) -> tuple[LiveRunner, ParquetStorage, RecordingAlerter]:
    storage = ParquetStorage(tmp_path / "data")
    risk = RiskGuard(fundarb_config.risk, fundarb_config.rebalance)
    executor = TwoLegExecutor(adapter, risk, leg_fill_timeout_sec=fundarb_config.risk.leg_fill_timeout_sec)
    strategy = CarryStrategy(
        fundarb_config.entry, exit_rule or fundarb_config.exit_rule, fundarb_config.risk.max_position_notional_usd
    )
    alerter = RecordingAlerter()
    runner = LiveRunner(
        venue=_VENUE,
        symbol=_SYMBOL,
        config=fundarb_config,
        adapter=adapter,
        storage=storage,
        risk=risk,
        executor=executor,
        strategy=strategy,
        alerter=alerter,
        metrics=MetricsRegistry(),
        journal=PositionJournal(tmp_path / "data"),
    )
    return runner, storage, alerter


@pytest.mark.asyncio
async def test_enters_when_apr_clears_threshold(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[filled_ack(_VENUE, "leg1", "0.1"), filled_ack(_VENUE, "leg2", "0.1")],
        quotes=_quotes(),
        funding_history=_high_yield_rates(),
    )
    runner, storage, _ = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())  # pre-seed so the very first cycle already has history

    await runner.start()
    await runner.run_once()

    assert runner.position is not None
    assert runner.position.spot_qty == Decimal("0.1")
    assert runner.position.perp_qty == Decimal("0.1")
    assert len(adapter.placed_intents) == 2
    assert adapter.set_leverage_calls == [(_SYMBOL, fundarb_config.risk.max_leverage)]

    journaled = runner.journal.load(_VENUE, _SYMBOL)
    assert journaled is not None
    assert journaled.spot_qty == Decimal("0.1")


@pytest.mark.asyncio
async def test_entry_skipped_when_leverage_check_fails(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        quotes=_quotes(),
        funding_history=_high_yield_rates(),
        perp_margin_balance=Decimal("10"),  # far too little for 5000 notional at 2x max leverage
    )
    runner, storage, _ = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()

    assert runner.position is None
    assert adapter.placed_intents == []


@pytest.mark.asyncio
async def test_live_collection_writes_new_funding_data_to_storage(tmp_path, fundarb_config) -> None:
    """Regression test for the core live-loop gap: cumulative_funding_pnl
    must actually update without a separate `collect` process running.
    """
    recent_rate = FundingRate(
        venue=_VENUE,
        symbol=_SYMBOL,
        funding_time=datetime.now(timezone.utc) - timedelta(minutes=5),
        rate=Decimal("0.0001"),
        interval_hours=8,
        mark_price=_SPOT_PRICE,
    )
    adapter = FakeAdapter(quotes=_quotes(), funding_history=[recent_rate])
    runner, storage, _ = _build_runner(tmp_path, fundarb_config, adapter)

    assert storage.read_funding_rates_typed(_VENUE, _SYMBOL) == []
    await runner.start()
    await runner.run_once()

    stored = storage.read_funding_rates_typed(_VENUE, _SYMBOL)
    assert len(stored) == 1
    assert stored[0].rate == Decimal("0.0001")


@pytest.mark.asyncio
async def test_fixed_profit_exit_closes_position(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(_VENUE, "entry-leg1", "0.1"),
            filled_ack(_VENUE, "entry-leg2", "0.1"),
            filled_ack(_VENUE, "exit-leg1", "0.1"),
            filled_ack(_VENUE, "exit-leg2", "0.1"),
        ],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, _ = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None
    entry_time = runner.position.entry_time

    # target = 0.6% of 5000 notional = 30; two 0.35% periods clear it
    storage.write_funding_rates(
        [
            FundingRate(
                venue=_VENUE,
                symbol=_SYMBOL,
                funding_time=entry_time + timedelta(seconds=1),
                rate=Decimal("0.0035"),
                interval_hours=8,
                mark_price=_SPOT_PRICE,
            ),
            FundingRate(
                venue=_VENUE,
                symbol=_SYMBOL,
                funding_time=entry_time + timedelta(seconds=2),
                rate=Decimal("0.0035"),
                interval_hours=8,
                mark_price=_SPOT_PRICE,
            ),
        ]
    )

    await runner.run_once()

    assert runner.position is None
    assert runner.journal.load(_VENUE, _SYMBOL) is None
    assert len(adapter.placed_intents) == 4
    assert adapter.placed_intents[-1].reason is IntentReason.EXIT_LEG1
    assert adapter.placed_intents[-1].market is Market.SPOT


@pytest.mark.asyncio
async def test_rate_reversal_exit_closes_position(tmp_path, fundarb_config, entry_config) -> None:
    exit_rule = ExitRuleConfig(
        mode=ExitRuleMode.RATE_REVERSAL,
        rate_reversal=RateReversalExit(consecutive_negative_periods=2, min_negative_rate_pct=Decimal(0)),
    )
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(_VENUE, "entry-leg1", "0.1"),
            filled_ack(_VENUE, "entry-leg2", "0.1"),
            filled_ack(_VENUE, "exit-leg1", "0.1"),
            filled_ack(_VENUE, "exit-leg2", "0.1"),
        ],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, _ = _build_runner(tmp_path, fundarb_config, adapter, exit_rule=exit_rule)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None
    entry_time = runner.position.entry_time

    storage.write_funding_rates(
        [
            FundingRate(
                venue=_VENUE,
                symbol=_SYMBOL,
                funding_time=entry_time + timedelta(seconds=i + 1),
                rate=Decimal("-0.0001"),
                interval_hours=8,
                mark_price=_SPOT_PRICE,
            )
            for i in range(2)
        ]
    )

    await runner.run_once()

    assert runner.position is None
    assert len(adapter.placed_intents) == 4


@pytest.mark.asyncio
async def test_auto_delta_rebalance_trims_oversized_leg(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(_VENUE, "entry-leg1", "0.1"),
            filled_ack(_VENUE, "entry-leg2", "0.1"),
            filled_ack(_VENUE, "rebalance", "0.02"),
        ],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None

    # perp price jumps: perp notional 0.1*60000=6000 vs spot 0.1*50000=5000 -> 16.7% deviation
    adapter.quotes = _quotes(spot=_SPOT_PRICE, perp=Decimal("60000"))

    await runner.run_once()

    assert len(adapter.placed_intents) == 3
    rebalance_intent = adapter.placed_intents[-1]
    assert rebalance_intent.reason is IntentReason.DELTA_REBALANCE
    assert rebalance_intent.side is OrderSide.BUY  # spot was the undersized leg relative to perp
    assert runner.position.spot_qty == Decimal("0.12")
    assert any(call[0] == "delta_out_of_tolerance" and call[1][2] is True for call in alerter.calls)


@pytest.mark.asyncio
async def test_kill_switch_closes_open_position_immediately(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(_VENUE, "entry-leg1", "0.1"),
            filled_ack(_VENUE, "entry-leg2", "0.1"),
            filled_ack(_VENUE, "close-leg1", "0.1"),
            filled_ack(_VENUE, "close-leg2", "0.1"),
        ],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None

    runner.risk.trigger_kill_switch("daily loss limit breached")
    await runner.run_once()

    assert runner.position is None
    assert runner.journal.load(_VENUE, _SYMBOL) is None
    assert runner.risk.kill_switch_active  # still active: manual reset required
    assert adapter.placed_intents[-1].reason is IntentReason.KILL_SWITCH_CLOSE
    assert any(call[0] == "send" and "kill-switch close succeeded" in call[1][0] for call in alerter.calls)


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_entries_when_flat(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(quotes=_quotes(), funding_history=[])
    runner, storage, _ = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    runner.risk.trigger_kill_switch("test")
    await runner.run_once()

    assert runner.position is None
    assert adapter.placed_intents == []


@pytest.mark.asyncio
async def test_connection_loss_marks_disconnected_and_alerts(tmp_path, fundarb_config) -> None:
    fundarb_config.monitor.connection_loss_alert_sec = 0  # alert on the very first failure
    adapter = FakeAdapter(raise_on_quote=ExchangeAdapterError("network unreachable"))
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)

    await runner.start()
    await runner.run_once()

    conn = runner.metrics.connections.get(_VENUE)
    assert conn is not None
    assert conn.connected is False
    assert any(call[0] == "connection_lost" for call in alerter.calls)


@pytest.mark.asyncio
async def test_position_metadata_survives_restart(tmp_path, fundarb_config) -> None:
    data_dir = tmp_path / "data"
    adapter1 = FakeAdapter(
        fill_responses=[filled_ack(_VENUE, "leg1", "0.1"), filled_ack(_VENUE, "leg2", "0.1")],
        quotes=_quotes(),
        funding_history=[],
    )
    storage1 = ParquetStorage(data_dir)
    storage1.write_funding_rates(_high_yield_rates())
    risk1 = RiskGuard(fundarb_config.risk, fundarb_config.rebalance)
    executor1 = TwoLegExecutor(adapter1, risk1, leg_fill_timeout_sec=fundarb_config.risk.leg_fill_timeout_sec)
    strategy1 = CarryStrategy(fundarb_config.entry, fundarb_config.exit_rule, fundarb_config.risk.max_position_notional_usd)
    runner1 = LiveRunner(
        venue=_VENUE,
        symbol=_SYMBOL,
        config=fundarb_config,
        adapter=adapter1,
        storage=storage1,
        risk=risk1,
        executor=executor1,
        strategy=strategy1,
        alerter=RecordingAlerter(),
        metrics=MetricsRegistry(),
        journal=PositionJournal(data_dir),
    )
    await runner1.start()
    await runner1.run_once()
    assert runner1.position is not None
    original_entry_time = runner1.position.entry_time
    original_entry_basis = runner1.position.entry_basis

    # "restart": fresh runner, fresh adapter instance, but the exchange
    # still shows the hedged position and the same data directory.
    adapter2 = FakeAdapter(
        quotes=_quotes(),
        funding_history=[],
        positions=[
            Position(
                venue=_VENUE,
                symbol=_SYMBOL,
                market=Market.PERP,
                side=OrderSide.SELL,
                quantity=Decimal("0.1"),
                entry_price=_PERP_PRICE,
                mark_price=_PERP_PRICE,
            )
        ],
        balances=Balances(venue=_VENUE, total={"BTC": Decimal("0.1")}, free={}, used={}),
    )
    storage2 = ParquetStorage(data_dir)
    risk2 = RiskGuard(fundarb_config.risk, fundarb_config.rebalance)
    executor2 = TwoLegExecutor(adapter2, risk2, leg_fill_timeout_sec=fundarb_config.risk.leg_fill_timeout_sec)
    strategy2 = CarryStrategy(fundarb_config.entry, fundarb_config.exit_rule, fundarb_config.risk.max_position_notional_usd)
    runner2 = LiveRunner(
        venue=_VENUE,
        symbol=_SYMBOL,
        config=fundarb_config,
        adapter=adapter2,
        storage=storage2,
        risk=risk2,
        executor=executor2,
        strategy=strategy2,
        alerter=RecordingAlerter(),
        metrics=MetricsRegistry(),
        journal=PositionJournal(data_dir),
    )
    await runner2.start()

    assert runner2.position is not None
    assert runner2.position.entry_time == original_entry_time
    assert runner2.position.entry_basis == original_entry_basis
    assert runner2.position.spot_qty == Decimal("0.1")
    assert runner2.position.perp_qty == Decimal("0.1")
