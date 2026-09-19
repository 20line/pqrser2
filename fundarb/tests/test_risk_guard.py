from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fundarb.core.models import OrderIntent
from fundarb.core.types import (
    IntentReason,
    Market,
    OrderSide,
    OrderType,
    RebalanceMode,
    RiskDecision,
    Venue,
)
from fundarb.risk.guard import RiskGuard

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _intent(
    quantity: str = "0.1",
    reduce_only: bool = False,
    reason: IntentReason = IntentReason.ENTRY_LEG1,
    market: Market = Market.SPOT,
) -> OrderIntent:
    return OrderIntent(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        market=market,
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        reduce_only=reduce_only,
        client_order_id="test-1",
        reason=reason,
    )


def test_approves_intent_within_limits(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    result = guard.check(
        _intent("0.01"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=_NOW,
    )
    assert result.approved


def test_rejects_intent_exceeding_position_size_limit(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    # 0.2 BTC @ 50000 = 10000 > max_position_notional_usd=5000
    result = guard.check(
        _intent("0.2"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=_NOW,
    )
    assert result.decision is RiskDecision.REJECTED
    assert "max_position_notional_usd" in result.reason


def test_rejects_intent_exceeding_total_exposure_limit(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    result = guard.check(
        _intent("0.01"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(19999),
        now=_NOW,
    )
    assert result.decision is RiskDecision.REJECTED
    assert "max_total_exposure_usd" in result.reason


def test_reduce_only_bypasses_size_and_exposure_checks(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    result = guard.check(
        _intent("0.2", reduce_only=True),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(999_999),
        now=_NOW,
    )
    assert result.approved


def test_order_frequency_limit(risk_config, rebalance_config) -> None:
    risk_config.max_orders_per_minute = 3
    guard = RiskGuard(risk_config, rebalance_config)
    for _ in range(3):
        result = guard.check(
            _intent("0.001"),
            price=Decimal("50000"),
            current_position_notional_usd=Decimal(0),
            total_exposure_usd=Decimal(0),
            now=_NOW,
        )
        assert result.approved
    result = guard.check(
        _intent("0.001"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=_NOW,
    )
    assert result.decision is RiskDecision.REJECTED
    assert "frequency" in result.reason


def test_order_frequency_window_slides(risk_config, rebalance_config) -> None:
    risk_config.max_orders_per_minute = 1
    guard = RiskGuard(risk_config, rebalance_config)
    result1 = guard.check(
        _intent("0.001"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=_NOW,
    )
    assert result1.approved
    later = _NOW + timedelta(seconds=61)
    result2 = guard.check(
        _intent("0.001"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=later,
    )
    assert result2.approved


def test_kill_switch_blocks_all_but_kill_switch_close_intents(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    guard.trigger_kill_switch("test reason")
    assert guard.kill_switch_active

    blocked = guard.check(
        _intent("0.001"),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=_NOW,
    )
    assert blocked.decision is RiskDecision.REJECTED
    assert "kill switch" in blocked.reason

    allowed = guard.check(
        _intent("0.001", reason=IntentReason.KILL_SWITCH_CLOSE),
        price=Decimal("50000"),
        current_position_notional_usd=Decimal(0),
        total_exposure_usd=Decimal(0),
        now=_NOW,
    )
    assert allowed.approved


def test_kill_switch_requires_manual_reset(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    guard.trigger_kill_switch("test reason")
    assert guard.kill_switch_active
    guard.reset_kill_switch()
    assert not guard.kill_switch_active


def test_daily_loss_limit_triggers_kill_switch(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    triggered = guard.record_realized_pnl(Decimal(-300), now=_NOW)
    assert not triggered
    assert not guard.kill_switch_active
    triggered = guard.record_realized_pnl(Decimal(-250), now=_NOW)
    assert triggered
    assert guard.kill_switch_active


def test_daily_loss_resets_on_new_day(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    guard.record_realized_pnl(Decimal(-450), now=_NOW)
    assert not guard.kill_switch_active
    next_day = _NOW + timedelta(days=1)
    triggered = guard.record_realized_pnl(Decimal(-450), now=next_day)
    assert not triggered


def test_check_leverage_rejects_above_max(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    result = guard.check_leverage(Decimal(10000), Decimal(4000))  # 2.5x > 2.0x max
    assert result.decision is RiskDecision.REJECTED


def test_check_leverage_approves_within_max(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    result = guard.check_leverage(Decimal(8000), Decimal(4000))  # 2.0x == max
    assert result.approved


# ---- delta rebalance (auto mode) ----------------------------------------


def test_check_delta_no_rebalance_within_tolerance(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    decision = guard.check_delta(
        spot_notional=Decimal(10000), perp_notional=Decimal(9950), now=_NOW
    )
    assert not decision.should_rebalance


def test_check_delta_triggers_auto_rebalance_beyond_tolerance(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    # spot 10000, perp 9000 -> 10% deviation > 1% tolerance
    decision = guard.check_delta(
        spot_notional=Decimal(10000), perp_notional=Decimal(9000), now=_NOW
    )
    assert decision.should_rebalance
    assert decision.side is OrderSide.SELL  # spot is the oversized leg
    assert decision.notional > 0


def test_check_delta_alert_only_mode_never_auto_rebalances(risk_config, rebalance_config) -> None:
    rebalance_config.mode = RebalanceMode.ALERT_ONLY
    guard = RiskGuard(risk_config, rebalance_config)
    decision = guard.check_delta(
        spot_notional=Decimal(10000), perp_notional=Decimal(9000), now=_NOW
    )
    assert not decision.should_rebalance
    assert decision.should_alert


def test_check_delta_respects_cooldown(risk_config, rebalance_config) -> None:
    guard = RiskGuard(risk_config, rebalance_config)
    first = guard.check_delta(spot_notional=Decimal(10000), perp_notional=Decimal(9000), now=_NOW)
    assert first.should_rebalance
    guard.record_rebalance(now=_NOW)

    soon_after = _NOW + timedelta(seconds=10)
    second = guard.check_delta(
        spot_notional=Decimal(10000), perp_notional=Decimal(9000), now=soon_after
    )
    assert not second.should_rebalance
    assert second.should_alert


def test_check_delta_rebalance_notional_capped(risk_config, rebalance_config) -> None:
    rebalance_config.max_rebalance_notional_pct = Decimal("10.0")
    guard = RiskGuard(risk_config, rebalance_config)
    # delta = 5000, position_notional = 10000, cap = 10% of 10000 = 1000
    decision = guard.check_delta(spot_notional=Decimal(10000), perp_notional=Decimal(5000), now=_NOW)
    assert decision.should_rebalance
    assert decision.notional == Decimal(1000)
