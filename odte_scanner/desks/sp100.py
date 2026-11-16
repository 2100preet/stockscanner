"""S&P 100 names for the paper desks.

Constituents as published for September 21, 2026 (101 tickers; Alphabet has
two share classes). ``BRK.B`` is stored as ``BRK-B`` so it matches Yahoo.
"""

from __future__ import annotations

# Wikipedia / iShares OEF constituents as of 2026-09-21.
SP100: tuple[str, ...] = (
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMAT", "AMD", "AMGN", "AMT", "AMZN",
    "ANET", "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BNY", "BRK-B",
    "C", "CAT", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS", "CVX",
    "DE", "DELL", "DHR", "DIS", "DUK", "EMR", "FDX", "GD", "GE", "GEV",
    "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "IBM", "INTC", "INTU", "ISRG",
    "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "LRCX", "MA", "MCD",
    "MDLZ", "MDT", "META", "MMM", "MO", "MRK", "MS", "MSFT", "MU", "NEE",
    "NFLX", "NOW", "NVDA", "ORCL", "PANW", "PEP", "PFE", "PG", "PLTR", "PM",
    "QCOM", "RTX", "SBUX", "SCHW", "SNDK", "SO", "T", "TMO", "TMUS", "TSLA",
    "TXN", "UBER", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT",
    "XOM",
)

# Oversold bounce is index-only. Single names did not hold up out of sample.
OVERSOLD_UNIVERSE: tuple[str, ...] = ("SPY", "QQQ")


def normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").upper().strip().replace(".", "-")
    if text == "BRK-B":
        return "BRK-B"
    return text


def sp100_universe() -> tuple[str, ...]:
    return SP100


def oversold_universe() -> tuple[str, ...]:
    """SPY and QQQ. The oversold bounce does not trade single stocks."""
    return OVERSOLD_UNIVERSE


def in_sp100(symbol: str) -> bool:
    return normalize_symbol(symbol) in set(SP100)


def in_oversold_universe(symbol: str) -> bool:
    return normalize_symbol(symbol) in set(oversold_universe())
