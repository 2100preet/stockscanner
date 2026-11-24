"""Read-only price source for the shadow desks.

Order: Tradier (production, real-time, already configured on Fly) ->
Yahoo 5-minute pre/post bars -> ONE Polygon aggregates call per name
(the Polygon key is shared with the live news desk at ~5/min, so it is
only a last resort and is never used in bulk).

Note: the Webull OpenAPI snapshot is not used server-side. The app's
Webull key is hard-locked to the paper sandbox host, and the live keys
belong to Sniper LIVE; neither is touched here.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time
from typing import Any

import requests

from odte_scanner.ext_hours.common import ET

logger = logging.getLogger(__name__)


def _bars_tradier(symbol: str, day: date, start: str, end: str) -> list[tuple[datetime, dict]]:
    from odte_scanner.data import tradier

    res = tradier.fetch_timesales(
        symbol, interval="5min", start=f"{day} {start}", end=f"{day} {end}", session_filter="all"
    )
    out = []
    for row in res.get("bars") or []:
        try:
            ts = datetime.fromisoformat(str(row.get("time"))).replace(tzinfo=ET)
        except Exception:  # noqa: BLE001
            continue
        out.append((ts, {k: float(row.get(k) or row.get("price") or 0) for k in ("open", "high", "low", "close")}))
    return out


def _bars_yahoo(symbol: str, day: date) -> list[tuple[datetime, dict]]:
    import yfinance as yf

    df = yf.download(symbol, period="5d", interval="5m", prepost=True, progress=False, multi_level_index=False)
    out = []
    for ts, r in (df if df is not None else []).iterrows():
        t = ts.tz_convert(ET).to_pydatetime()
        if t.date() == day:
            out.append((t, {"open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])}))
    return out


def _bars_polygon(symbol: str, day: date) -> list[tuple[datetime, dict]]:
    key = (os.environ.get("POLYGON_API_KEY") or os.environ.get("MASSIVE_API_KEY") or "").strip()
    if not key:
        return []
    r = requests.get(
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/5/minute/{day}/{day}",
        params={"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": key},
        timeout=15,
    )
    if r.status_code != 200:
        return []
    out = []
    for b in (r.json() or {}).get("results") or []:
        t = datetime.fromtimestamp(b["t"] / 1000, tz=ET)
        out.append((t, {"open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]}))
    return out


def _last_close_before(bars: list[tuple[datetime, dict]], cutoff: time) -> float | None:
    vals = [b["close"] for ts, b in bars if ts.time() < cutoff and b.get("close")]
    return vals[-1] if vals else None


class QuoteSource:
    """Injectable in tests. Every method fails soft (returns None/{})."""

    def __init__(self, *, use_polygon: bool = True) -> None:
        self.use_polygon = use_polygon
        self.polygon_calls = 0
        self._polygon_cache: dict[tuple[str, str], list] = {}  # at most ONE call per name per day

    # ---- live quotes -----------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        try:
            from odte_scanner.data import tradier

            res = tradier.fetch_quotes(list(symbols))
            q = res.get("quotes") or {}
            if q:
                return q
        except Exception as exc:  # noqa: BLE001
            logger.warning("shadow quotes via tradier failed: %s", type(exc).__name__)
        return {}

    # ---- intraday bars incl. extended hours --------------------------------
    def bars(self, symbol: str, day: date, start: str = "15:00", end: str = "20:00") -> list[tuple[datetime, dict]]:
        for name, fn in (("tradier", lambda: _bars_tradier(symbol, day, start, end)), ("yahoo", lambda: _bars_yahoo(symbol, day))):
            try:
                b = fn()
                if b:
                    return b
            except Exception as exc:  # noqa: BLE001
                logger.warning("shadow bars %s %s failed: %s", name, symbol, type(exc).__name__)
        if self.use_polygon:
            key = (symbol.upper(), day.isoformat())
            if key in self._polygon_cache:
                return self._polygon_cache[key]
            self.polygon_calls += 1
            try:
                got = _bars_polygon(symbol, day)
            except Exception as exc:  # noqa: BLE001
                logger.warning("shadow bars polygon %s failed: %s", symbol, type(exc).__name__)
                got = []
            self._polygon_cache[key] = got
            if len(self._polygon_cache) > 200:
                self._polygon_cache.pop(next(iter(self._polygon_cache)))
            return got
        return []

    def ah_prices(self, symbol: str, day: date) -> dict[str, float | None]:
        """C = 3:00 PM CT close, P4 = 4:00 PM CT price, P5 = 5:00 PM CT price (all ET bar times)."""
        bars = self.bars(symbol, day, "15:00", "18:05")
        return {
            "C": _last_close_before(bars, time(16, 0)),
            "P4": _last_close_before(bars, time(17, 0)),
            "P5": _last_close_before(bars, time(18, 0)),
        }

    def ah_range(self, symbol: str, day: date) -> dict[str, float | None]:
        """High/low printed 5:00–7:00 PM CT (18:00–20:00 ET) for the shadow fill check."""
        bars = [(ts, b) for ts, b in self.bars(symbol, day, "18:00", "20:00") if time(18, 0) <= ts.time() < time(20, 0)]
        if not bars:
            return {"high": None, "low": None}
        return {"high": max(b["high"] for _, b in bars), "low": min(b["low"] for _, b in bars)}
