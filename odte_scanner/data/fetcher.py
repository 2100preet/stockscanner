from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"


def _cache_path(symbol: str, period: str, interval: str) -> Path:
    safe = symbol.replace("^", "IDX_")
    return CACHE_DIR / f"{safe}_{period}_{interval}.parquet"


def fetch_history(
    symbol: str,
    *,
    period: str = "6mo",
    interval: str = "1d",
    use_cache: bool = True,
    cache_max_age_hours: int = 6,
    yahoo_symbol: str | None = None,
) -> pd.DataFrame:
    """Download OHLCV history for a symbol via yfinance."""
    fetch_sym = yahoo_symbol or symbol
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(fetch_sym, period, interval)

    if use_cache and path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        if age < timedelta(hours=cache_max_age_hours):
            df = pd.read_parquet(path)
            if not df.empty:
                return df

    logger.info("Downloading %s (%s / %s)", fetch_sym, period, interval)
    ticker = yf.Ticker(fetch_sym)
    df = ticker.history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        logger.warning("No data for %s", symbol)
        return df

    df = df.rename(columns=str.title)
    # Normalize timezone-aware index to naive dates for daily bars
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index.name = "Date"

    if use_cache:
        try:
            df.to_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cache write failed for %s: %s", symbol, exc)

    return df


def fetch_many(
    symbols: Iterable[str],
    *,
    period: str = "6mo",
    interval: str = "1d",
    aliases: dict[str, str] | None = None,
) -> dict[str, pd.DataFrame]:
    aliases = aliases or {}
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        display = str(sym).upper().lstrip("^")
        yahoo = aliases.get(display, aliases.get(sym, sym))
        df = fetch_history(display, period=period, interval=interval, yahoo_symbol=yahoo)
        if not df.empty:
            out[display] = df
    return out


def fetch_intraday(symbol: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    return fetch_history(symbol, period=period, interval=interval, cache_max_age_hours=1)


def latest_quote(symbol: str) -> dict:
    t = yf.Ticker(symbol)
    info = {}
    try:
        fast = t.fast_info
        info = {
            "symbol": symbol,
            "last": float(getattr(fast, "last_price", None) or 0),
            "prev_close": float(getattr(fast, "previous_close", None) or 0),
            "day_high": float(getattr(fast, "day_high", None) or 0),
            "day_low": float(getattr(fast, "day_low", None) or 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Quote failed for %s: %s", symbol, exc)
        hist = fetch_history(symbol, period="5d")
        if not hist.empty:
            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else last
            info = {
                "symbol": symbol,
                "last": float(last["Close"]),
                "prev_close": float(prev["Close"]),
                "day_high": float(last["High"]),
                "day_low": float(last["Low"]),
            }
    return info
