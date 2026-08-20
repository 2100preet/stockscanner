"""Session scorer for names the old desk never even scanned.

MRNA on 2026-08-19 gapped ~+84% on a Phase 3 melanoma readout and closed +177%.
That is not an 80% hist-win 0DTE card. It is a catalyst + volume + gap event.
This module scores that class of tape so it cannot be silently dropped.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

import pandas as pd

NEWS_RE = re.compile(
    r"\b("
    r"phase\s*3|phase\s*iii|phase\s*2|trial|fda|approved|approval|"
    r"vaccine|cancer|melanoma|breakthrough|readout|endpoint|"
    r"statistically significant|pdufa|crl|complete response|"
    r"earnings|beats|misses|guidance|acquisition|buyout|merger|"
    r"halted|halt|investigation|recall"
    r")\b",
    re.I,
)

LANE_DO_NOT_MISS = "DO_NOT_MISS"
LANE_CATALYST = "CATALYST"
LANE_TAPE = "TAPE"
LANE_QUIET = "QUIET"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def news_hits(titles: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for raw in titles:
        t = str(raw or "").strip()
        if t and NEWS_RE.search(t):
            hits.append(t)
    return hits[:6]


def score_session(
    df: pd.DataFrame,
    *,
    symbol: str,
    news_titles: Iterable[str] = (),
) -> dict[str, Any]:
    """Score the latest daily bar vs prior close.

    Pure function — tests feed a DataFrame, no Yahoo.
    """
    sym = str(symbol).upper()
    empty = {
        "symbol": sym,
        "lane": LANE_QUIET,
        "miss_score": 0.0,
        "gap_pct": None,
        "day_change_pct": None,
        "rel_volume": None,
        "last": None,
        "prev_close": None,
        "volume": None,
        "avg_volume": None,
        "news_hits": [],
        "why": "no bars",
        "side": "flat",
        "action": "NO_DATA",
        "risk": "Cannot score without history.",
    }
    if df is None or getattr(df, "empty", True) or len(df) < 2:
        return empty

    work = df.copy()
    cols = {c.lower(): c for c in work.columns}
    close_col = cols.get("close") or cols.get("adj close")
    open_col = cols.get("open")
    vol_col = cols.get("volume")
    if not close_col:
        return empty

    last = work.iloc[-1]
    prev = work.iloc[-2]
    last_close = _f(last[close_col])
    prev_close = _f(prev[close_col])
    last_open = _f(last[open_col]) if open_col else last_close
    volume = _f(last[vol_col]) if vol_col else 0.0
    avg_vol = 0.0
    if vol_col and len(work) > 2:
        avg_vol = _f(work[vol_col].iloc[:-1].tail(20).mean())

    if prev_close <= 0 or last_close <= 0:
        return empty

    gap_pct = (last_open - prev_close) / prev_close * 100.0
    day_change_pct = (last_close - prev_close) / prev_close * 100.0
    rel_volume = (volume / avg_vol) if avg_vol > 0 else None
    hits = news_hits(news_titles)
    abs_move = max(abs(gap_pct), abs(day_change_pct))
    side = "long" if day_change_pct >= 0 else "short"

    miss = 0.0
    miss += min(0.45, abs(gap_pct) / 100.0)
    miss += min(0.25, abs(day_change_pct) / 80.0)
    if rel_volume:
        miss += min(0.20, math.log10(max(rel_volume, 1.0)) / 2.0)
    if hits:
        miss += 0.15
    miss = round(min(1.0, miss), 3)

    if abs(gap_pct) >= 8.0 or abs(day_change_pct) >= 12.0 or (rel_volume or 0) >= 5.0:
        lane = LANE_DO_NOT_MISS
        action = "SEE_TAPE"
    elif hits and abs_move >= 3.0:
        lane = LANE_CATALYST
        action = "READ_NEWS"
    elif (rel_volume or 0) >= 2.0 and abs_move >= 3.0:
        lane = LANE_TAPE
        action = "WATCH"
    else:
        lane = LANE_QUIET
        action = "IGNORE"

    bits = [
        f"gap {gap_pct:+.1f}%",
        f"vs prior {day_change_pct:+.1f}%",
    ]
    if rel_volume:
        bits.append(f"{rel_volume:.1f}x vol")
    if hits:
        bits.append("news catalyst")
    why = " · ".join(bits)

    risk = (
        "Binary biotech / event tape. The same pattern that prints +177% can print −70% "
        "on a failed trial. This is a miss-prevention flag, not a buy order."
        if lane == LANE_DO_NOT_MISS
        else "Size small or stand aside until the tape confirms. Not a guarantee."
    )

    return {
        "symbol": sym,
        "lane": lane,
        "miss_score": miss,
        "gap_pct": round(gap_pct, 2),
        "day_change_pct": round(day_change_pct, 2),
        "rel_volume": round(rel_volume, 2) if rel_volume is not None else None,
        "last": round(last_close, 4),
        "open": round(last_open, 4),
        "prev_close": round(prev_close, 4),
        "volume": int(volume) if volume else 0,
        "avg_volume": int(avg_vol) if avg_vol else 0,
        "news_hits": hits,
        "why": why,
        "side": side,
        "action": action,
        "risk": risk,
    }


def fetch_yahoo_headlines(symbol: str, *, limit: int = 6) -> list[str]:
    """Best-effort Yahoo headlines. Never raises."""
    try:
        import yfinance as yf

        news = yf.Ticker(symbol).news or []
    except Exception:  # noqa: BLE001
        return []
    titles: list[str] = []
    for item in news[: limit * 2]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not title:
            content = item.get("content") or {}
            if isinstance(content, dict):
                title = content.get("title")
        if title:
            titles.append(str(title))
        if len(titles) >= limit:
            break
    return titles
