"""Symbol normalization. Exchanges spell tickers, separators, and perpetual
markers differently (BTCUSDT vs BTC/USDT:USDT, etc). The rest of the system
only ever sees the normalized "BASE/QUOTE" form; adapters translate at the
boundary in both directions.
"""

from __future__ import annotations

from fundarb.core.types import Market


def normalize(base: str, quote: str) -> str:
    return f"{base.upper()}/{quote.upper()}"


def split(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    if not quote:
        raise ValueError(f"not a normalized symbol: {symbol!r}")
    return base, quote


def to_ccxt_symbol(symbol: str, market: Market) -> str:
    """Normalized "BASE/QUOTE" -> ccxt unified symbol for spot or perp."""
    base, quote = split(symbol)
    if market is Market.SPOT:
        return f"{base}/{quote}"
    return f"{base}/{quote}:{quote}"


def from_ccxt_symbol(ccxt_symbol: str) -> str:
    """ccxt unified symbol (spot or perp) -> normalized "BASE/QUOTE"."""
    left = ccxt_symbol.split(":", 1)[0]
    base, quote = left.split("/", 1)
    return normalize(base, quote)
