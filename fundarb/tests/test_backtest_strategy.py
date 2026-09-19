from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fundarb.backtest.strategy import CarryStrategy, OpenPosition
from fundarb.config import ExitRuleConfig, FixedProfitExit, RateReversalExit
from fundarb.core.types import ExitRuleMode, Venue

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _position(notional: str = "10000", entry_basis: str = "0.001") -> OpenPosition:
    return OpenPosition(
        venue=Venue.BINANCE,
        symbol="BTC/USDT",
        entry_time=_NOW,
        entry_basis=Decimal(entry_basis),
        notional=Decimal(notional),
        entry_rate_apr_pct=Decimal("20"),
    )


def test_decide_entry_rejects_below_threshold(entry_config, exit_rule_config) -> None:
    strategy = CarryStrategy(entry_config, exit_rule_config, Decimal(5000))
    decision = strategy.decide_entry(net_apr_estimate_pct=Decimal("10"), basis_bps=Decimal("2"))
    assert not decision.should_enter


def test_decide_entry_accepts_at_or_above_threshold(entry_config, exit_rule_config) -> None:
    strategy = CarryStrategy(entry_config, exit_rule_config, Decimal(5000))
    decision = strategy.decide_entry(net_apr_estimate_pct=Decimal("15"), basis_bps=Decimal("2"))
    assert decision.should_enter
    assert decision.notional == Decimal(5000)


def test_on_funding_event_accumulates_pnl_and_tracks_streak(entry_config) -> None:
    exit_cfg = ExitRuleConfig(
        mode=ExitRuleMode.RATE_REVERSAL,
        rate_reversal=RateReversalExit(consecutive_negative_periods=2, min_negative_rate_pct=Decimal(0)),
    )
    strategy = CarryStrategy(entry_config, exit_cfg, Decimal(5000))
    position = _position()

    strategy.on_funding_event(position, Decimal("0.0002"))
    assert position.cumulative_funding_pnl == Decimal("0.0002") * position.notional
    assert position.consecutive_negative_periods == 0

    strategy.on_funding_event(position, Decimal("-0.0001"))
    assert position.consecutive_negative_periods == 1

    strategy.on_funding_event(position, Decimal("0.0001"))
    assert position.consecutive_negative_periods == 0  # streak resets on a positive period


def test_rate_reversal_exit_after_consecutive_negative_periods(entry_config) -> None:
    exit_cfg = ExitRuleConfig(
        mode=ExitRuleMode.RATE_REVERSAL,
        rate_reversal=RateReversalExit(consecutive_negative_periods=3, min_negative_rate_pct=Decimal(0)),
    )
    strategy = CarryStrategy(entry_config, exit_cfg, Decimal(5000))
    position = _position()

    for _ in range(2):
        strategy.on_funding_event(position, Decimal("-0.0001"))
        decision = strategy.decide_exit(position, current_basis=position.entry_basis, latest_rate=Decimal("-0.0001"))
        assert not decision.should_exit

    strategy.on_funding_event(position, Decimal("-0.0001"))
    decision = strategy.decide_exit(position, current_basis=position.entry_basis, latest_rate=Decimal("-0.0001"))
    assert decision.should_exit
    assert "consecutive" in decision.reason


def test_rate_reversal_does_not_exit_while_rate_stays_positive(entry_config) -> None:
    exit_cfg = ExitRuleConfig(
        mode=ExitRuleMode.RATE_REVERSAL,
        rate_reversal=RateReversalExit(consecutive_negative_periods=3, min_negative_rate_pct=Decimal(0)),
    )
    strategy = CarryStrategy(entry_config, exit_cfg, Decimal(5000))
    position = _position()

    for _ in range(10):
        strategy.on_funding_event(position, Decimal("0.0002"))
        decision = strategy.decide_exit(position, current_basis=position.entry_basis, latest_rate=Decimal("0.0002"))
        assert not decision.should_exit


def test_fixed_profit_exit_triggers_at_target(entry_config) -> None:
    exit_cfg = ExitRuleConfig(
        mode=ExitRuleMode.FIXED_PROFIT,
        fixed_profit=FixedProfitExit(target_pct_of_notional=Decimal("0.5")),
    )
    strategy = CarryStrategy(entry_config, exit_cfg, Decimal(5000))
    position = _position(notional="10000")

    # target = 0.5% of 10000 = 50
    strategy.on_funding_event(position, Decimal("0.003"))  # 0.003 * 10000 = 30
    decision = strategy.decide_exit(position, current_basis=position.entry_basis, latest_rate=Decimal("0.003"))
    assert not decision.should_exit  # only 30 accrued, below 50 target

    strategy.on_funding_event(position, Decimal("0.003"))  # +30 -> total 60 >= 50
    decision = strategy.decide_exit(position, current_basis=position.entry_basis, latest_rate=Decimal("0.003"))
    assert decision.should_exit


def test_fixed_profit_exit_accounts_for_basis_drag(entry_config) -> None:
    """Entering when perp trades above spot (positive basis) and exiting
    after the basis has widened further should ERODE the funding gains —
    this is the ΔB term from the spec's formula, not just raw funding sum.
    """
    exit_cfg = ExitRuleConfig(
        mode=ExitRuleMode.FIXED_PROFIT,
        fixed_profit=FixedProfitExit(target_pct_of_notional=Decimal("0.5")),
    )
    strategy = CarryStrategy(entry_config, exit_cfg, Decimal(5000))
    position = _position(notional="10000", entry_basis="0.001")

    strategy.on_funding_event(position, Decimal("0.006"))  # 60 in funding, would clear target alone
    # basis widened from 0.001 to 0.006 -> costs (0.006-0.001)*10000 = 50 -> net pnl only 10
    decision = strategy.decide_exit(position, current_basis=Decimal("0.006"), latest_rate=Decimal("0.006"))
    assert not decision.should_exit

    # basis instead narrowed to 0.0 -> basis_pnl = +0.001*10000 = 10 -> total 70 >= 50
    decision2 = strategy.decide_exit(position, current_basis=Decimal("0.0"), latest_rate=Decimal("0.006"))
    assert decision2.should_exit
