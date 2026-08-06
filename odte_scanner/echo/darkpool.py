"""Dark pool / ATS levels from FINRA OTC Transparency (public API).

Source: https://api.finra.org/data/group/otcMarket/name/weeklySummary
Docs: https://www.finra.org/filing-reporting/otc-transparency

Notes:
  • Tier-1 ATS weekly data is published with ~2 week delay (not a live tape).
  • FINRA does not publish print prices — "levels" are volume-profile magnets
    from Yahoo bars, emphasized when ATS volume is surging.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

FINRA_WEEKLY = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
_CACHE_DIR = Path(__file__).resolve().parents[2] / "outputs" / "echo_darkpool"
_CACHE_TTL_SEC = 6 * 3600  # FINRA updates weekly; refresh a few times/day


def _monday_on_or_before(d: datetime | None = None) -> datetime:
    d = d or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d - timedelta(days=d.weekday())


def _finra_post(payload: dict[str, Any], *, timeout: float = 45.0) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        FINRA_WEEKLY,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SignalDesk-Echo/1.0 (research; FINRA public OTC transparency)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if not body or resp.status == 204:
                return []
            data = json.loads(body)
            return data if isinstance(data, list) else []
    except urllib.error.HTTPError as exc:
        logger.debug("FINRA HTTP %s: %s", exc.code, exc.read()[:200])
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("FINRA request failed: %s", exc)
        return []


def _cache_get(key: str, ttl: int = _CACHE_TTL_SEC) -> Any | None:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        if time.time() - float(raw.get("_cached_at") or 0) > ttl:
            return None
        return raw.get("data")
    except Exception:  # noqa: BLE001
        return None


def _cache_set(key: str, data: Any) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{key}.json").write_text(
            json.dumps({"_cached_at": time.time(), "data": data})
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("darkpool cache write failed: %s", exc)


def latest_published_week(symbol: str = "SPY", *, lookback_weeks: int = 8) -> str | None:
    """Find the most recent FINRA weekStartDate with ATS symbol data."""
    cached = _cache_get("latest_week")
    if cached:
        return str(cached)
    monday = _monday_on_or_before()
    for i in range(1, lookback_weeks + 1):
        wk = (monday - timedelta(days=7 * i)).date().isoformat()
        rows = _finra_post(
            {
                "compareFilters": [
                    {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol},
                    {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL"},
                    {"compareType": "EQUAL", "fieldName": "weekStartDate", "fieldValue": wk},
                ],
                "limit": 1,
            }
        )
        if rows:
            _cache_set("latest_week", wk)
            return wk
    # Fallback: GREATER scan
    start = (monday - timedelta(days=7 * 20)).date().isoformat()
    rows = _finra_post(
        {
            "compareFilters": [
                {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol},
                {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL"},
                {"compareType": "GREATER", "fieldName": "weekStartDate", "fieldValue": start},
            ],
            "limit": 100,
        }
    )
    if not rows:
        return None
    wk = max(str(r.get("weekStartDate")) for r in rows if r.get("weekStartDate"))
    _cache_set("latest_week", wk)
    return wk


def fetch_symbol_ats_history(
    symbol: str,
    *,
    weeks: int = 6,
    week_start: str | None = None,
) -> list[dict[str, Any]]:
    """ATS_W_SMBL weekly history for one symbol (aggregate across venues)."""
    cache_key = f"hist_{symbol.upper()}_{weeks}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    latest = week_start or latest_published_week(symbol)
    if not latest:
        return []
    start_dt = datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=7 * max(1, weeks))
    rows = _finra_post(
        {
            "compareFilters": [
                {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol.upper()},
                {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL"},
                {
                    "compareType": "GREATER",
                    "fieldName": "weekStartDate",
                    "fieldValue": start_dt.date().isoformat(),
                },
            ],
            "limit": 100,
        }
    )
    # Dedupe by week (keep max shares if duplicates)
    by_week: dict[str, dict[str, Any]] = {}
    for r in rows:
        wk = str(r.get("weekStartDate") or "")
        if not wk:
            continue
        prev = by_week.get(wk)
        shares = int(r.get("totalWeeklyShareQuantity") or 0)
        if prev is None or shares >= int(prev.get("totalWeeklyShareQuantity") or 0):
            by_week[wk] = r
    out = [by_week[k] for k in sorted(by_week.keys())]
    _cache_set(cache_key, out)
    return out


def fetch_symbol_venues(symbol: str, *, week_start: str) -> list[dict[str, Any]]:
    """ATS venue breakdown for one symbol/week."""
    cache_key = f"venues_{symbol.upper()}_{week_start}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows = _finra_post(
        {
            "compareFilters": [
                {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol.upper()},
                {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL_FIRM"},
                {"compareType": "EQUAL", "fieldName": "weekStartDate", "fieldValue": week_start},
            ],
            "limit": 100,
        }
    )
    rows.sort(key=lambda r: int(r.get("totalWeeklyShareQuantity") or 0), reverse=True)
    _cache_set(cache_key, rows)
    return rows


def compute_volume_magnets(
    df: pd.DataFrame,
    *,
    bins: int = 24,
    lookback: int = 30,
) -> list[dict[str, Any]]:
    """Volume-at-price magnets from Yahoo OHLCV (proxy levels — not FINRA print prices)."""
    if df is None or df.empty or len(df) < 5:
        return []
    window = df.tail(lookback).copy()
    lo = float(window["Low"].min())
    hi = float(window["High"].max())
    if hi <= lo:
        return []
    edges = np.linspace(lo, hi, bins + 1)
    vol_at = np.zeros(bins)
    for _, row in window.iterrows():
        # distribute bar volume across the price range of the bar
        a = float(row["Low"])
        b = float(row["High"])
        v = float(row.get("Volume") or 0)
        if v <= 0 or b <= a:
            # put at close
            c = float(row["Close"])
            idx = min(bins - 1, max(0, int((c - lo) / (hi - lo) * bins)))
            vol_at[idx] += v
            continue
        for i in range(bins):
            left, right = edges[i], edges[i + 1]
            overlap = max(0.0, min(b, right) - max(a, left))
            if overlap > 0:
                vol_at[i] += v * (overlap / (b - a))
    total = float(vol_at.sum()) or 1.0
    ranked = sorted(range(bins), key=lambda i: vol_at[i], reverse=True)
    last = float(window["Close"].iloc[-1])
    levels: list[dict[str, Any]] = []
    for rank, i in enumerate(ranked[:6]):
        price = float((edges[i] + edges[i + 1]) / 2)
        share = 100.0 * vol_at[i] / total
        if rank == 0:
            tag = "hero"  # point of control / highest volume node
        elif price >= last:
            tag = "resistance" if rank <= 2 else "magnet"
        else:
            tag = "support" if rank <= 2 else "magnet"
        levels.append(
            {
                "price": round(price, 2),
                "volume_share_pct": round(share, 2),
                "tag": tag,
                "dist_pct": round((price - last) / last * 100.0, 3) if last else None,
            }
        )
    levels.sort(key=lambda x: x["price"])
    return levels


def _summarize_symbol(
    symbol: str,
    history: list[dict[str, Any]],
    venues: list[dict[str, Any]],
    *,
    magnets: list[dict[str, Any]] | None = None,
    spot: float | None = None,
) -> dict[str, Any] | None:
    if not history:
        return None
    hist = sorted(history, key=lambda r: str(r.get("weekStartDate") or ""))
    latest = hist[-1]
    prev = hist[-2] if len(hist) >= 2 else None
    shares = int(latest.get("totalWeeklyShareQuantity") or 0)
    trades = int(latest.get("totalWeeklyTradeCount") or 0)
    notional = float(latest.get("totalNotionalSum") or 0)
    prev_shares = int(prev.get("totalWeeklyShareQuantity") or 0) if prev else None
    wow = None
    if prev_shares and prev_shares > 0:
        wow = round((shares - prev_shares) / prev_shares * 100.0, 1)
    # trailing average (ex-latest)
    trail = [int(r.get("totalWeeklyShareQuantity") or 0) for r in hist[:-1]]
    trail_avg = float(np.mean(trail)) if trail else None
    surge_ratio = round(shares / trail_avg, 2) if trail_avg and trail_avg > 0 else None
    flag = "normal"
    if surge_ratio is not None:
        if surge_ratio >= 1.5:
            flag = "surge"
        elif surge_ratio <= 0.5:
            flag = "drop"
    top_venues = []
    for v in venues[:8]:
        top_venues.append(
            {
                "mpid": v.get("MPID"),
                "name": v.get("marketParticipantName") or v.get("MPID"),
                "shares": int(v.get("totalWeeklyShareQuantity") or 0),
                "trades": int(v.get("totalWeeklyTradeCount") or 0),
                "avg_trade_size": (
                    round(
                        int(v.get("totalWeeklyShareQuantity") or 0)
                        / max(1, int(v.get("totalWeeklyTradeCount") or 1)),
                        1,
                    )
                ),
            }
        )
    return {
        "symbol": symbol.upper(),
        "week_start": latest.get("weekStartDate"),
        "published": latest.get("initialPublishedDate") or latest.get("lastUpdateDate"),
        "shares": shares,
        "trades": trades,
        "notional": round(notional, 2),
        "avg_trade_size": round(shares / max(1, trades), 1),
        "wow_pct": wow,
        "surge_ratio": surge_ratio,
        "flag": flag,
        "spot": spot,
        "venues": top_venues,
        "levels": magnets or [],
        "history": [
            {
                "week_start": r.get("weekStartDate"),
                "shares": int(r.get("totalWeeklyShareQuantity") or 0),
                "trades": int(r.get("totalWeeklyTradeCount") or 0),
            }
            for r in hist[-6:]
        ],
    }


def build_darkpool_board(
    symbols: list[str],
    *,
    histories: dict[str, pd.DataFrame] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    max_symbols: int = 12,
) -> dict[str, Any]:
    """Dark pool desk: FINRA ATS volume + volume-magnet levels."""
    quotes = quotes or {}
    histories = histories or {}
    week = latest_published_week("SPY")
    rows: list[dict[str, Any]] = []
    venue_leaders: dict[str, int] = defaultdict(int)

    for sym in symbols[:max_symbols]:
        hist = fetch_symbol_ats_history(sym, weeks=6, week_start=week)
        if not hist:
            continue
        latest_wk = str(hist[-1].get("weekStartDate") or week or "")
        venues = fetch_symbol_venues(sym, week_start=latest_wk) if latest_wk else []
        magnets = []
        df = histories.get(sym)
        if df is not None:
            magnets = compute_volume_magnets(df)
        spot = None
        q = quotes.get(sym) or {}
        if q.get("last") is not None:
            spot = float(q["last"])
        elif df is not None and not df.empty:
            spot = float(df["Close"].iloc[-1])
        summary = _summarize_symbol(sym, hist, venues, magnets=magnets, spot=spot)
        if not summary:
            continue
        rows.append(summary)
        for v in summary.get("venues") or []:
            name = str(v.get("name") or v.get("mpid") or "")
            if name:
                venue_leaders[name] += int(v.get("shares") or 0)

    rows.sort(
        key=lambda r: (
            0 if r.get("flag") == "surge" else 1,
            -(r.get("surge_ratio") or 0),
            -(r.get("shares") or 0),
        )
    )
    top_venues = sorted(venue_leaders.items(), key=lambda x: -x[1])[:10]

    return {
        "available": True,
        "source": "FINRA OTC Transparency API (weeklySummary)",
        "source_url": "https://www.finra.org/filing-reporting/otc-transparency",
        "api": FINRA_WEEKLY,
        "week_start": week,
        "delay_note": (
            "Tier-1 ATS data is typically published with ~2 weeks delay. "
            "This is official dark-pool / ATS volume — not a live print tape."
        ),
        "levels_note": (
            "Price levels are volume-profile magnets from Yahoo OHLCV (hero/support/resistance/magnet tags). "
            "FINRA does not publish ATS print prices publicly."
        ),
        "rows": rows,
        "surges": [r for r in rows if r.get("flag") == "surge"],
        "drops": [r for r in rows if r.get("flag") == "drop"],
        "top_venues": [{"name": n, "shares": s} for n, s in top_venues],
        "counts": {
            "symbols": len(rows),
            "surges": sum(1 for r in rows if r.get("flag") == "surge"),
            "drops": sum(1 for r in rows if r.get("flag") == "drop"),
        },
    }
