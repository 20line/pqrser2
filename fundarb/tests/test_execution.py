from __future__ import annotations

from decimal import Decimal

import pytest

from fundarb.core.errors import OrderTimeoutError
from fundarb.core.types import IntentReason, Market, OrderSide, Venue
from fundarb.execution.executor import TwoLegExecutor, deterministic_client_order_id
from fundarb.risk.guard import RiskGuard
from tests.fakes import FakeAdapter, filled_ack


def _executor(adapter: FakeAdapter, risk_config, rebalance_config) -> TwoLegExecutor:
    risk = RiskGuard(risk_config, rebalance_config)
    return TwoLegExecutor(adapter, risk, leg_fill_timeout_sec=5)


@pytest.mark.asyncio
async def test_successful_two_leg_entry(risk_config, rebalance_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(Venue.BINANCE, "leg1", "0.1"),
            filled_ack(Venue.BINANCE, "leg2", "0.1"),
        ]
    )
    executor = _executor(adapter, risk_config, rebalance_config)

    result = await executor.execute(
        symbol="BTC/USDT",
        leg1_market=Market.SPOT,
        leg1_side=OrderSide.BUY,
        leg2_market=Market.PERP,
        leg2_side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price_leg1=Decimal("50000"),
        price_leg2=Decimal("49990"),
        reason=IntentReason.ENTRY_LEG1,
        epoch_key="epoch-1",
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
    )

    assert result.success
    assert not result.rolled_back
    assert len(adapter.placed_intents) == 2
    assert result.leg2.ack.filled_quantity == Decimal("0.1")


@pytest.mark.asyncio
async def test_leg2_sized_to_leg1_actual_fill_not_requested_quantity(
    risk_config, rebalance_config
) -> None:
    """Leg1 requested 0.1 but only fills 0.07 (partial fill) -> leg2 must
    be sized to 0.07, not the original 0.1.
    """
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(Venue.BINANCE, "leg1", "0.07"),
            filled_ack(Venue.BINANCE, "leg2", "0.07"),
        ]
    )
    executor = _executor(adapter, risk_config, rebalance_config)

    result = await executor.execute(
        symbol="BTC/USDT",
        leg1_market=Market.SPOT,
        leg1_side=OrderSide.BUY,
        leg2_market=Market.PERP,
        leg2_side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price_leg1=Decimal("50000"),
        price_leg2=Decimal("49990"),
        reason=IntentReason.ENTRY_LEG1,
        epoch_key="epoch-1",
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
    )

    assert result.success
    assert adapter.placed_intents[1].quantity == Decimal("0.07")


@pytest.mark.asyncio
async def test_leg2_timeout_rolls_back_leg1(risk_config, rebalance_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(Venue.BINANCE, "leg1", "0.1"),
            OrderTimeoutError("leg2 never filled"),
            filled_ack(Venue.BINANCE, "rollback", "0.1"),
        ]
    )
    executor = _executor(adapter, risk_config, rebalance_config)

    result = await executor.execute(
        symbol="BTC/USDT",
        leg1_market=Market.SPOT,
        leg1_side=OrderSide.BUY,
        leg2_market=Market.PERP,
        leg2_side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price_leg1=Decimal("50000"),
        price_leg2=Decimal("49990"),
        reason=IntentReason.ENTRY_LEG1,
        epoch_key="epoch-1",
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
    )

    assert not result.success
    assert result.rolled_back
    assert not executor.risk.kill_switch_active
    # leg1, leg2 (failed), rollback = 3 placed orders
    assert len(adapter.placed_intents) == 3
    # rollback must be the opposite side of leg1 (leg1 was BUY spot -> rollback SELL spot)
    rollback_intent = adapter.placed_intents[-1]
    assert rollback_intent.side is OrderSide.SELL
    assert rollback_intent.market is Market.SPOT
    assert rollback_intent.reduce_only is True


@pytest.mark.asyncio
async def test_rollback_failure_triggers_kill_switch(risk_config, rebalance_config) -> None:
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(Venue.BINANCE, "leg1", "0.1"),
            OrderTimeoutError("leg2 never filled"),
            OrderTimeoutError("rollback also failed"),
        ]
    )
    executor = _executor(adapter, risk_config, rebalance_config)

    result = await executor.execute(
        symbol="BTC/USDT",
        leg1_market=Market.SPOT,
        leg1_side=OrderSide.BUY,
        leg2_market=Market.PERP,
        leg2_side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price_leg1=Decimal("50000"),
        price_leg2=Decimal("49990"),
        reason=IntentReason.ENTRY_LEG1,
        epoch_key="epoch-1",
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
    )

    assert not result.success
    assert not result.rolled_back
    assert executor.risk.kill_switch_active


@pytest.mark.asyncio
async def test_leg1_failure_needs_no_rollback(risk_config, rebalance_config) -> None:
    adapter = FakeAdapter(fill_responses=[OrderTimeoutError("leg1 never filled")])
    executor = _executor(adapter, risk_config, rebalance_config)

    result = await executor.execute(
        symbol="BTC/USDT",
        leg1_market=Market.SPOT,
        leg1_side=OrderSide.BUY,
        leg2_market=Market.PERP,
        leg2_side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price_leg1=Decimal("50000"),
        price_leg2=Decimal("49990"),
        reason=IntentReason.ENTRY_LEG1,
        epoch_key="epoch-1",
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
    )

    assert not result.success
    assert not result.rolled_back
    assert result.leg1 is None
    assert len(adapter.placed_intents) == 1


@pytest.mark.asyncio
async def test_leg2_rejected_by_risk_guard_rolls_back_without_placing_leg2(
    risk_config, rebalance_config
) -> None:
    """leg1 fills at a price that pushes leg2's notional over the position
    cap, so leg2 must never reach the exchange — only leg1 + rollback.
    """
    risk_config.max_position_notional_usd = Decimal("1000")
    adapter = FakeAdapter(
        fill_responses=[
            filled_ack(Venue.BINANCE, "leg1", "0.1"),
            filled_ack(Venue.BINANCE, "rollback", "0.1"),
        ]
    )
    executor = _executor(adapter, risk_config, rebalance_config)

    result = await executor.execute(
        symbol="BTC/USDT",
        leg1_market=Market.SPOT,
        leg1_side=OrderSide.BUY,
        leg2_market=Market.PERP,
        leg2_side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price_leg1=Decimal("500"),  # leg1 risk check passes: 0.1*500=50 < 1000
        price_leg2=Decimal("50000"),  # leg2 risk check fails: 0.1*50000=5000 > 1000
        reason=IntentReason.ENTRY_LEG1,
        epoch_key="epoch-1",
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
    )

    assert not result.success
    assert result.rolled_back
    assert len(adapter.placed_intents) == 2  # leg1 + rollback, leg2 never placed


def test_deterministic_client_order_id_is_stable_for_same_inputs() -> None:
    id1 = deterministic_client_order_id(Venue.BINANCE, "BTC/USDT", IntentReason.ENTRY_LEG1, "epoch-1", 1)
    id2 = deterministic_client_order_id(Venue.BINANCE, "BTC/USDT", IntentReason.ENTRY_LEG1, "epoch-1", 1)
    id3 = deterministic_client_order_id(Venue.BINANCE, "BTC/USDT", IntentReason.ENTRY_LEG1, "epoch-1", 2)
    assert id1 == id2
    assert id1 != id3
