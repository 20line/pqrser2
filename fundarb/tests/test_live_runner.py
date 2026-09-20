from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fundarb.backtest.strategy import CarryStrategy
from fundarb.collect.storage import ParquetStorage
from fundarb.config import ExitRuleConfig, FundarbConfig, RateReversalExit
from fundarb.core.errors import ExchangeAdapterError, StorageError
from fundarb.core.models import Balances, FundingRate, Position, Quote
from fundarb.core.types import ExitRuleMode, IntentReason, Market, OrderSide, Venue
from fundarb.execution.executor import TwoLegExecutor
from fundarb.execution.journal import PositionJournal
from fundarb.execution.ledger import TradeLedger
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
        fundarb_config.entry,
        exit_rule or fundarb_config.exit_rule,
        fundarb_config.risk.max_position_notional_usd,
        fundarb_config.universe.max_spread_bps,
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
        ledger=TradeLedger(tmp_path / "data"),
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
    assert (_VENUE, _SYMBOL) not in runner.metrics.positions  # stale snapshot must not linger

    ledger_rows = runner.ledger.read_all()
    assert ledger_rows.height == 1
    row = ledger_rows.row(0, named=True)
    assert row["exit_reason"] == IntentReason.EXIT_LEG1.value
    assert Decimal(row["funding_pnl"]) == Decimal("35.0")  # 2 * 0.0035 * 5000
    assert Decimal(row["basis_pnl"]) == Decimal(0)  # quotes never moved
    assert Decimal(row["realized_pnl"]) == Decimal("35.0")


@pytest.mark.asyncio
async def test_margin_low_alert_fires_below_threshold(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[filled_ack(_VENUE, "entry-leg1", "0.1"), filled_ack(_VENUE, "entry-leg2", "0.1")],
        quotes=_quotes(),
        funding_history=[],
        perp_margin_balance=Decimal("100000"),  # plenty for the leverage check at entry
    )
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None

    # margin drops to 10% of the 5000 notional -> below margin_alert_ratio=0.3
    adapter.perp_margin_balance = Decimal("500")
    await runner.run_once()

    assert any(call[0] == "margin_low" for call in alerter.calls)
    metrics = runner.metrics.positions[(_VENUE, _SYMBOL)]
    assert metrics.margin_ratio == Decimal("500") / Decimal("5000")


@pytest.mark.asyncio
async def test_rate_reversal_alert_fires_regardless_of_exit_rule_mode(tmp_path, fundarb_config) -> None:
    """fixed_profit is the default exit rule here — the alert must still
    fire on a negative funding print even though the strategy won't exit
    on it alone.
    """
    adapter = FakeAdapter(
        fill_responses=[filled_ack(_VENUE, "entry-leg1", "0.1"), filled_ack(_VENUE, "entry-leg2", "0.1")],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
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
                funding_time=entry_time + timedelta(seconds=1),
                rate=Decimal("-0.0001"),
                interval_hours=8,
                mark_price=_SPOT_PRICE,
            )
        ]
    )
    await runner.run_once()

    assert runner.position is not None  # fixed_profit: this alone doesn't trigger an exit
    assert any(call[0] == "rate_reversal" for call in alerter.calls)


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
async def test_stop_loss_forces_close_on_unrealized_loss(tmp_path, fundarb_config) -> None:
    """max_unrealized_loss_usd fires on basis drag alone, with zero funding
    events applied and the strategy's own exit rule nowhere close to
    triggering — this is a position-level guard independent of the
    strategy's exit logic.
    """
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(_VENUE, "entry-leg1", "0.1"),
            filled_ack(_VENUE, "entry-leg2", "0.1"),
            filled_ack(_VENUE, "stop-leg1", "0.1"),
            filled_ack(_VENUE, "stop-leg2", "0.1"),
        ],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None

    # perp blows out to 56000: basis_pnl ~= -601, past max_unrealized_loss_usd=300
    adapter.quotes = _quotes(spot=_SPOT_PRICE, perp=Decimal("56000"))

    await runner.run_once()

    assert runner.position is None
    assert runner.journal.load(_VENUE, _SYMBOL) is None
    assert len(adapter.placed_intents) == 4
    assert adapter.placed_intents[-1].reason is IntentReason.STOP_LOSS_CLOSE
    assert any(call[0] == "send" and "stop-loss triggered" in call[1][0] for call in alerter.calls)
    # the ~$601 realized loss also breaches daily_loss_limit_usd=500 ->
    # record_realized_pnl chains into the kill switch, blocking new entries
    assert runner.risk.kill_switch_active
    # ...and the operator gets an explicit kill-switch push, not just the
    # stop-loss message above
    assert any(call[0] == "kill_switch" for call in alerter.calls)


@pytest.mark.asyncio
async def test_auto_delta_rebalance_trims_oversized_leg(tmp_path, fundarb_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(_VENUE, "entry-leg1", "0.1"),
            filled_ack(_VENUE, "entry-leg2", "0.1"),
            filled_ack(_VENUE, "rebalance", "0.002"),
        ],
        quotes=_quotes(),
        funding_history=[],
    )
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())

    await runner.start()
    await runner.run_once()
    assert runner.position is not None

    # perp price moves 2%: perp notional 0.1*51000=5100 vs spot 0.1*50000=5000 ->
    # 1.96% deviation (clears the 1% rebalance bar) while the basis-driven
    # unrealized loss (~$100) stays well under the $300 stop-loss bar.
    adapter.quotes = _quotes(spot=_SPOT_PRICE, perp=Decimal("51000"))

    await runner.run_once()

    assert len(adapter.placed_intents) == 3
    rebalance_intent = adapter.placed_intents[-1]
    assert rebalance_intent.reason is IntentReason.DELTA_REBALANCE
    assert rebalance_intent.side is OrderSide.BUY  # spot was the undersized leg relative to perp
    assert runner.position.spot_qty == Decimal("0.102")
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
    strategy1 = CarryStrategy(
        fundarb_config.entry, fundarb_config.exit_rule, fundarb_config.risk.max_position_notional_usd, fundarb_config.universe.max_spread_bps
    )
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
        ledger=TradeLedger(data_dir),
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
    strategy2 = CarryStrategy(
        fundarb_config.entry, fundarb_config.exit_rule, fundarb_config.risk.max_position_notional_usd, fundarb_config.universe.max_spread_bps
    )
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
        ledger=TradeLedger(data_dir),
    )
    await runner2.start()

    assert runner2.position is not None
    assert runner2.position.entry_time == original_entry_time
    assert runner2.position.entry_basis == original_entry_basis
    assert runner2.position.spot_qty == Decimal("0.1")
    assert runner2.position.perp_qty == Decimal("0.1")


class _FailingLedger:
    """Stands in for TradeLedger — always raises on append, to verify a
    ledger write failure doesn't leave position state stale or crash the
    cycle (see live_runner.py::_execute_close).
    """

    def append(self, entry) -> None:
        raise StorageError("disk full")

    def read_all(self):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_ledger_write_failure_does_not_leave_position_stale_or_crash(tmp_path, fundarb_config) -> None:
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
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    runner.ledger = _FailingLedger()
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
                rate=Decimal("0.0035"),
                interval_hours=8,
                mark_price=_SPOT_PRICE,
            )
            for i in range(2)
        ]
    )

    await runner.run_once()  # must not raise despite the ledger failure

    # the exchange legs closed successfully -> position state must reflect
    # that even though the ledger write failed
    assert runner.position is None
    assert runner.journal.load(_VENUE, _SYMBOL) is None
    assert any(call[0] == "send" and "ledger write failed" in call[1][0] for call in alerter.calls)


@pytest.mark.asyncio
async def test_unhandled_exception_in_cycle_is_caught_and_alerted(tmp_path, fundarb_config, monkeypatch) -> None:
    adapter = FakeAdapter(quotes=_quotes(), funding_history=[])
    runner, storage, alerter = _build_runner(tmp_path, fundarb_config, adapter)
    storage.write_funding_rates(_high_yield_rates())
    await runner.start()

    async def _boom(*args, **kwargs):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(runner, "_try_enter", _boom)

    await runner.run_once()  # must not raise

    assert runner.position is None
    assert any(call[0] == "send" and "unhandled error" in call[1][0] for call in alerter.calls)
