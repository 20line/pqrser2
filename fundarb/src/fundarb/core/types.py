"""Enums shared across the whole system."""

from __future__ import annotations

from enum import StrEnum


class Venue(StrEnum):
    BINANCE = "binance"
    BYBIT = "bybit"


class Market(StrEnum):
    SPOT = "spot"
    PERP = "perp"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ExitRuleMode(StrEnum):
    FIXED_PROFIT = "fixed_profit"
    RATE_REVERSAL = "rate_reversal"


class RebalanceMode(StrEnum):
    AUTO = "auto"
    ALERT_ONLY = "alert_only"


class IntentReason(StrEnum):
    ENTRY_LEG1 = "entry_leg1"
    ENTRY_LEG2 = "entry_leg2"
    ENTRY_ROLLBACK = "entry_rollback"
    EXIT_LEG1 = "exit_leg1"
    EXIT_LEG2 = "exit_leg2"
    DELTA_REBALANCE = "delta_rebalance"
    KILL_SWITCH_CLOSE = "kill_switch_close"


class RiskDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
