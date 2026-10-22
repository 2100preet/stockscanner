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


def _from_tradier_row(symbol: str, row: dict[str, Any]) -> LiveQuote | None:
    try:
        from odte_scanner.data.tradier import quote_to_live_dict

        d = quote_to_live_dict(row, symbol=symbol)
        if not d or not d.get("last"):
            return None
        return LiveQuote(
            symbol=symbol,
            last=float(d["last"]),
            prev_close=float(d["prev_close"]),
            change=float(d["change"]),
            change_pct=float(d["change_pct"]),
            session=str(d.get("session") or "regular"),
            asof=str(d.get("asof") or datetime.now(timezone.utc).isoformat()),
            day_high=d.get("day_high"),
            day_low=d.get("day_low"),
            session_open=d.get("session_open"),
            session_change=d.get("session_change"),
            session_change_pct=d.get("session_change_pct"),
            mom_5m_pct=d.get("mom_5m_pct"),
            mom_15m_pct=d.get("mom_15m_pct"),
            dist_from_day_high_pct=d.get("dist_from_day_high_pct"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tradier quote map %s: %s", symbol, exc)
        return None


def fetch_live_quote(symbol: str, *, yahoo_symbol: str | None = None) -> LiveQuote | None:
    """Best-effort last price — Tradier first when token set, then Yahoo, then cache.

    Falls back to cached daily bars when live endpoints fail.
    """
    fetch_sym = yahoo_symbol or symbol
    # Prefer Tradier production marks when TRADIER_ACCESS_TOKEN is set
    try:
        from odte_scanner.data.tradier import access_token_from_env, fetch_quotes

        if access_token_from_env():
            tq = fetch_quotes([str(fetch_sym).upper()], timeout=10.0)
            row = (tq.get("quotes") or {}).get(str(fetch_sym).upper())
            if tq.get("ok") and row:
                mapped = _from_tradier_row(symbol, row)
                if mapped:
                    return mapped
    except Exception as exc:  # noqa: BLE001
        logger.debug("tradier equity quote fallback %s: %s", symbol, exc)

    # Polygon / Massive equity snapshot backup
    try:
        from odte_scanner.data.polygon import api_key_from_env as poly_key
        from odte_scanner.data.polygon import fetch_equity_quote as poly_eq

        if poly_key():
            pq = poly_eq(str(fetch_sym).upper())
            if pq and pq.get("last"):
                mapped = _from_tradier_row(symbol, pq)  # same field shape
                if mapped:
                    return mapped
    except Exception as exc:  # noqa: BLE001
        logger.debug("polygon equity quote fallback %s: %s", symbol, exc)

    # Prefer chart API (crumb session) — more reliable under yfinance 429s
    try:
        from odte_scanner.options.yahoo_session import fetch_yahoo_quote

        yq = fetch_yahoo_quote(fetch_sym)
        if yq and yq.get("last"):
            last = float(yq["last"])
            prev = float(yq.get("prev_close") or last)
            chg = last - prev
            return LiveQuote(
                symbol=symbol,
                last=last,
                prev_close=prev,
                change=chg,
                change_pct=(chg / prev * 100.0) if prev else 0.0,
                session=str(yq.get("session") or "regular"),
                asof=str(yq.get("asof") or datetime.now(timezone.utc).isoformat()),
                day_high=float(yq["day_high"]) if yq.get("day_high") else None,
                day_low=float(yq["day_low"]) if yq.get("day_low") else None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("yahoo chart quote path %s: %s", symbol, exc)

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


def board_quote_symbols(
    rows: list[dict[str, Any]] | None,
    *,
    extra: list[str] | None = None,
    min_score: float = 60.0,
    limit: int = 40,
) -> list[str]:
    """Symbols on the options board that need a live tape.

    Score around 60+ is included in full. Alphabetical slicing used to quote
    only eight names and leave the rest with a null session change.
    """
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        try:
            score = float(row.get("score") if row.get("score") is not None else row.get("ensemble_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score < float(min_score):
            continue
        seen.add(sym)
        scored.append((score, sym))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = [sym for _score, sym in scored]
    for raw in extra or []:
        sym = str(raw or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    cap = max(1, int(limit))
    return out[:cap]


def momentum_from_closes(closes: list[float]) -> tuple[float | None, float | None]:
    """5-minute and 15-minute percent moves from ordered closes. None when short."""
    mom5 = mom15 = None
    if len(closes) >= 2 and closes[-2] > 0:
        mom5 = (closes[-1] / closes[-2] - 1.0) * 100.0
    if len(closes) >= 4 and closes[-4] > 0:
        mom15 = (closes[-1] / closes[-4] - 1.0) * 100.0
    return mom5, mom15


def _momentum_from_timesales(symbol: str) -> tuple[float | None, float | None]:
    from datetime import timedelta

    from odte_scanner.data.tradier import ET, fetch_timesales

    now = datetime.now(ET)
    start = (now - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M")
    end = now.strftime("%Y-%m-%d %H:%M")
    res = fetch_timesales(symbol, interval="5min", start=start, end=end, timeout=8.0)
    closes: list[float] = []
    for row in res.get("bars") or []:
        if not isinstance(row, dict):
            continue
        try:
            px = float(row.get("close") or row.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            closes.append(px)
    return momentum_from_closes(closes)


def _attach_missing_momentum(quotes: dict[str, LiveQuote]) -> None:
    """Fill 5m/15m from Tradier timesales when the quote itself has no momentum."""
    missing = [sym for sym, quote in quotes.items() if quote.mom_5m_pct is None]
    if not missing:
        return
    try:
        from odte_scanner.data.tradier import access_token_from_env

        if not access_token_from_env():
            return
    except Exception:  # noqa: BLE001
        return
    from concurrent.futures import ThreadPoolExecutor

    def _one(sym: str) -> tuple[str, tuple[float | None, float | None]]:
        try:
            return sym, _momentum_from_timesales(sym)
        except Exception as exc:  # noqa: BLE001
            logger.debug("momentum timesales %s: %s", sym, exc)
            return sym, (None, None)

    workers = min(6, len(missing))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sym, (mom5, mom15) in pool.map(_one, missing):
            quote = quotes.get(sym)
            if quote is None:
                continue
            if mom5 is not None:
                quote.mom_5m_pct = mom5
            if mom15 is not None and quote.mom_15m_pct is None:
                quote.mom_15m_pct = mom15


def fetch_live_quotes(
    symbols: list[str],
    *,
    aliases: dict[str, str] | None = None,
    attach_momentum: bool = False,
) -> dict[str, LiveQuote]:
    aliases = aliases or {}
    out: dict[str, LiveQuote] = {}
    # Batch Tradier when available (one POST vs N Yahoo calls)
    try:
        from odte_scanner.data.tradier import access_token_from_env, fetch_quotes

        if access_token_from_env() and symbols:
            fetch_syms = []
            back_map: dict[str, str] = {}
            for sym in symbols:
                fs = str(aliases.get(sym) or sym).upper()
                fetch_syms.append(fs)
                back_map.setdefault(fs, sym)
            tq = fetch_quotes(fetch_syms, timeout=15.0)
            # Accept partial batches — one failed chunk must not discard good quotes
            for fs, row in (tq.get("quotes") or {}).items():
                desk_sym = back_map.get(fs, fs)
                mapped = _from_tradier_row(desk_sym, row)
                if mapped:
                    out[desk_sym] = mapped
    except Exception as exc:  # noqa: BLE001
        logger.debug("tradier batch quotes failed: %s", exc)

    for sym in symbols:
        if sym in out:
            continue
        q = fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))
        if q:
            out[sym] = q
    if attach_momentum:
        _attach_missing_momentum(out)
    return out
