from __future__ import annotations

from decimal import Decimal

import pytest

from fundarb.config import (
    EntryConfig,
    ExitRuleConfig,
    FeesConfig,
    RebalanceConfig,
    RiskConfig,
    VenueFees,
)


@pytest.fixture
def fees_config() -> FeesConfig:
    return FeesConfig(
        binance=VenueFees(
            spot_taker_bps=Decimal(10),
            spot_maker_bps=Decimal(8),
            perp_taker_bps=Decimal(5),
            perp_maker_bps=Decimal(2),
        ),
        bybit=VenueFees(
            spot_taker_bps=Decimal(10),
            spot_maker_bps=Decimal(8),
            perp_taker_bps=Decimal(6),
            perp_maker_bps=Decimal(1),
        ),
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_position_notional_usd=Decimal(5000),
        max_total_exposure_usd=Decimal(20000),
        max_leverage=Decimal("2.0"),
        daily_loss_limit_usd=Decimal(500),
        max_orders_per_minute=20,
        leg_fill_timeout_sec=10,
    )


@pytest.fixture
def rebalance_config() -> RebalanceConfig:
    return RebalanceConfig(
        mode="auto",
        max_delta_deviation_pct=Decimal("1.0"),
        min_rebalance_interval_sec=300,
        max_rebalance_notional_pct=Decimal("50.0"),
    )


@pytest.fixture
def entry_config() -> EntryConfig:
    return EntryConfig(
        min_net_apr_pct=Decimal("15.0"),
        min_history_days=90,
        max_negative_period_share=Decimal("0.25"),
        max_funding_drawdown_pct=Decimal("5.0"),
    )


@pytest.fixture
def exit_rule_config() -> ExitRuleConfig:
    return ExitRuleConfig()
