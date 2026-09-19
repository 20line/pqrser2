"""Exceptions used across the system. Keep them narrow and specific —
a broad `except Exception` around exchange calls hides the one failure mode
(a hung leg) that this system cannot tolerate.
"""

from __future__ import annotations


class FundarbError(Exception):
    """Base class for all fundarb-specific errors."""


class ExchangeAdapterError(FundarbError):
    """An exchange call failed (network, auth, rejected order, ...)."""


class OrderTimeoutError(ExchangeAdapterError):
    """An order did not reach a terminal state within the configured timeout."""


class ReconciliationError(FundarbError):
    """Local state could not be reconciled with the exchange's actual state."""


class RiskRejected(FundarbError):
    """Raised when code path assumes an approved intent but got a rejection."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StorageError(FundarbError):
    """Parquet read/write failure."""


class ConfigError(FundarbError):
    """Invalid or missing configuration."""
