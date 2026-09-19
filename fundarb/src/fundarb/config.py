"""Typed configuration loaded from YAML, with secrets coming from env vars
(pydantic-settings). This is the file that encodes the five decisions from
the spec's "Что предстоит решить" list — see config/config.yaml for the
commentary on each.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from fundarb.core.types import ExitRuleMode, RebalanceMode, Venue


class UniverseConfig(BaseModel):
    venues: list[Venue] = [Venue.BINANCE, Venue.BYBIT]
    quote_currencies: list[str] = ["USDT"]
    mode: str = "volume_filter"
    min_24h_quote_volume_usd: Decimal = Decimal("20000000")
    min_contract_age_days: int = 30
    max_spread_bps: Decimal = Decimal(5)
    exclude_symbols: list[str] = Field(default_factory=list)


class EntryConfig(BaseModel):
    min_net_apr_pct: Decimal = Decimal("15.0")
    min_history_days: int = 90
    max_negative_period_share: Decimal = Decimal("0.25")
    max_funding_drawdown_pct: Decimal = Decimal("5.0")


class FixedProfitExit(BaseModel):
    target_pct_of_notional: Decimal = Decimal("0.6")


class RateReversalExit(BaseModel):
    consecutive_negative_periods: int = 3
    min_negative_rate_pct: Decimal = Decimal("0.0")


class ExitRuleConfig(BaseModel):
    mode: ExitRuleMode = ExitRuleMode.FIXED_PROFIT
    fixed_profit: FixedProfitExit = FixedProfitExit()
    rate_reversal: RateReversalExit = RateReversalExit()


class RebalanceConfig(BaseModel):
    mode: RebalanceMode = RebalanceMode.AUTO
    max_delta_deviation_pct: Decimal = Decimal("1.0")
    min_rebalance_interval_sec: int = 300
    max_rebalance_notional_pct: Decimal = Decimal("50.0")


class RiskConfig(BaseModel):
    max_position_notional_usd: Decimal = Decimal("5000")
    max_total_exposure_usd: Decimal = Decimal("20000")
    max_leverage: Decimal = Decimal("2.0")
    daily_loss_limit_usd: Decimal = Decimal("500")
    # Position-level stop-loss on unrealized PnL (funding + basis drag),
    # checked every cycle. Deliberately tighter than daily_loss_limit_usd:
    # this closes ONE bad position before it alone could exhaust the day's
    # account-wide loss budget.
    max_unrealized_loss_usd: Decimal = Decimal("300")
    max_orders_per_minute: int = 20
    leg_fill_timeout_sec: int = 10


class VenueFees(BaseModel):
    spot_taker_bps: Decimal
    spot_maker_bps: Decimal
    perp_taker_bps: Decimal
    perp_maker_bps: Decimal


class FeesConfig(BaseModel):
    binance: VenueFees
    bybit: VenueFees

    def for_venue(self, venue: Venue) -> VenueFees:
        return getattr(self, venue.value)


class MonitorConfig(BaseModel):
    telegram_enabled: bool = False
    connection_loss_alert_sec: int = 60
    margin_alert_ratio: Decimal = Decimal("0.3")


class FundarbConfig(BaseModel):
    universe: UniverseConfig = UniverseConfig()
    entry: EntryConfig = EntryConfig()
    exit_rule: ExitRuleConfig = ExitRuleConfig()
    rebalance: RebalanceConfig = RebalanceConfig()
    risk: RiskConfig = RiskConfig()
    fees: FeesConfig
    monitor: MonitorConfig = MonitorConfig()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FundarbConfig":
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
        return cls.model_validate(raw)


class Secrets(BaseSettings):
    """API credentials and alert tokens — env only, never in config.yaml."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_config(path: str | Path | None = None) -> FundarbConfig:
    resolved = Path(path or os.environ.get("FUNDARB_CONFIG", "config/config.yaml"))
    return FundarbConfig.from_yaml(resolved)


def load_secrets() -> Secrets:
    return Secrets()
