from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class LiveQuote:
    symbol: str
    last: float
    prev_close: float
    change: float
    change_pct: float
    session: str  # regular | prepost | overnight | unknown
    asof: str
    day_high: float | None = None
    day_low: float | None = None
    # Robinhood-style 24h mark: change vs first extended-hours print of the session day
    session_open: float | None = None
    session_change: float | None = None
    session_change_pct: float | None = None
    # Short-horizon momentum (percent)
    mom_5m_pct: float | None = None
    mom_15m_pct: float | None = None
    dist_from_day_high_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _session_label(ts: pd.Timestamp) -> str:
    # US/Eastern hour if tz-aware; else treat as unknown
    try:
        if ts.tzinfo is not None:
            et = ts.tz_convert("America/New_York")
            h = et.hour + et.minute / 60
            if 9.5 <= h < 16:
                return "regular"
            if 4 <= h < 9.5 or 16 <= h < 20:
                return "prepost"
            return "overnight"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _quote_from_daily_cache(symbol: str, *, yahoo_symbol: str | None = None) -> LiveQuote | None:
    """Fallback spot from local parquet / daily history when Yahoo live endpoints are blocked."""
    try:
        from pathlib import Path

        import pandas as pd

        from odte_scanner.data.fetcher import CACHE_DIR, fetch_history

        fetch_sym = yahoo_symbol or symbol
        safe = str(fetch_sym).replace("^", "IDX_")
        df = None
        # Prefer any existing local parquet (avoid Yahoo while rate-limited)
        if CACHE_DIR.exists():
            candidates = sorted(
                CACHE_DIR.glob(f"{safe}_*_1d.parquet"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in candidates:
                try:
                    cached = pd.read_parquet(path)
                    if cached is not None and not cached.empty:
                        df = cached
                        break
                except Exception:  # noqa: BLE001
                    continue

        if df is None or df.empty:
            # Last resort: fetcher (may hit network if cache stale/missing)
            for period in ("3mo", "2y", "6mo"):
                try:
                    df = fetch_history(
                        symbol,
                        period=period,
                        interval="1d",
                        use_cache=True,
                        cache_max_age_hours=168,
                        yahoo_symbol=yahoo_symbol,
                    )
                except Exception:  # noqa: BLE001
                    df = None
                if df is not None and not df.empty:
                    break
        if df is None or df.empty:
            return None
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) > 1 else last_row
        last = float(last_row["Close"])
        prev_close = float(prev_row["Close"])
        if last <= 0 or prev_close <= 0:
            return None
        asof = str(df.index[-1])
        chg = last - prev_close
        return LiveQuote(
            symbol=symbol,
            last=last,
            prev_close=prev_close,
            change=chg,
            change_pct=(chg / prev_close) * 100,
            session="cache",
            asof=asof,
            day_high=float(last_row.get("High") or 0) or None,
            day_low=float(last_row.get("Low") or 0) or None,
            session_open=float(last_row.get("Open") or 0) or None,
            session_change=chg,
            session_change_pct=(chg / prev_close) * 100,
            mom_5m_pct=None,
            mom_15m_pct=None,
            dist_from_day_high_pct=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily cache quote failed for %s: %s", symbol, exc)
        return None


def fetch_live_quote(symbol: str, *, yahoo_symbol: str | None = None) -> LiveQuote | None:
    """Best-effort last price including extended / overnight bars when Yahoo has them.

    Falls back to cached daily bars when Yahoo rate-limits live endpoints.
    """
    fetch_sym = yahoo_symbol or symbol
    try:
        t = yf.Ticker(fetch_sym)
        prev_close = None
        last = None
        day_high = day_low = None
        session = "unknown"
        asof = datetime.now(timezone.utc).isoformat()
        session_open = None

        try:
            fi = t.fast_info
            last = float(getattr(fi, "last_price", None) or 0) or None
            prev_close = float(getattr(fi, "previous_close", None) or 0) or None
            day_high = float(getattr(fi, "day_high", None) or 0) or None
            day_low = float(getattr(fi, "day_low", None) or 0) or None
            session = "regular"
        except Exception:  # noqa: BLE001
            pass

        # Prefer latest prepost/overnight 1m bar when available (Robinhood-style 24h tape)
        mom_5m = mom_15m = None
        try:
            hist = t.history(period="2d", interval="1m", prepost=True)
            if hist is not None and not hist.empty:
                bar = hist.iloc[-1]
                last = float(bar["Close"])
                asof = str(bar.name)
                session = _session_label(bar.name) if hasattr(bar.name, "tzinfo") else "prepost"
                # prior regular close from daily
                daily = t.history(period="10d", interval="1d")
                if daily is not None and not daily.empty:
                    prev_close = float(daily["Close"].iloc[-2] if len(daily) > 1 else daily["Close"].iloc[-1])
                # Session open ≈ first bar of the current ET calendar day (24h market mark)
                try:
                    et_idx = hist.index.tz_convert("America/New_York") if hist.index.tz is not None else hist.index
                    today = et_idx[-1].date()
                    tod = hist[et_idx.date == today]
                    if len(tod):
                        session_open = float(tod["Open"].iloc[0])
                        day_high = float(tod["High"].max())
                        day_low = float(tod["Low"].min())
                except Exception:  # noqa: BLE001
                    day_high = float(hist["High"].iloc[-390:].max()) if len(hist) else day_high
                    day_low = float(hist["Low"].iloc[-390:].min()) if len(hist) else day_low
                # Short-horizon momentum
                if len(hist) >= 6:
                    mom_5m = (float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[-6]) - 1) * 100
                if len(hist) >= 16:
                    mom_15m = (float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[-16]) - 1) * 100
        except Exception as exc:  # noqa: BLE001
            logger.debug("prepost quote failed for %s: %s", fetch_sym, exc)

        if last is None or prev_close is None or prev_close == 0:
            return _quote_from_daily_cache(symbol, yahoo_symbol=yahoo_symbol)

        chg = last - prev_close
        sess_chg = (last - session_open) if session_open else None
        sess_pct = ((sess_chg / session_open) * 100) if session_open else None
        dist_high = None
        if day_high and day_high > 0 and last:
            dist_high = (last / day_high - 1) * 100
        return LiveQuote(
            symbol=symbol,
            last=last,
            prev_close=prev_close,
            change=chg,
            change_pct=(chg / prev_close) * 100,
            session=session,
            asof=asof,
            day_high=day_high,
            day_low=day_low,
            session_open=session_open,
            session_change=sess_chg,
            session_change_pct=sess_pct,
            mom_5m_pct=mom_5m,
            mom_15m_pct=mom_15m,
            dist_from_day_high_pct=dist_high,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live quote failed for %s: %s", symbol, exc)
        return _quote_from_daily_cache(symbol, yahoo_symbol=yahoo_symbol)


def fetch_live_quotes(
    symbols: list[str],
    *,
    aliases: dict[str, str] | None = None,
) -> dict[str, LiveQuote]:
    aliases = aliases or {}
    out: dict[str, LiveQuote] = {}
    for sym in symbols:
        q = fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))
        if q:
            out[sym] = q
    return out
