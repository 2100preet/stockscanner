"""Liquid-universe market board ranked by earnings proximity and volume.

Yahoo cannot support a true full-market scan from this app, so we cover the
curated liquid optionable universe (~200 names) and warm the earnings cache
progressively across snapshot refreshes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.challenge.earnings import earnings_map_for, scan_earnings_calendar
from odte_scanner.data.fetcher import CACHE_DIR
from odte_scanner.data.universe import (
    FOCUS_DEFAULT,
    earnings_darlings_universe,
    liquid_universe,
    market_cap_tier,
)

logger = logging.getLogger(__name__)

BUCKET_RANK = {"today": 0, "this_week": 1, "next_week": 2, "post": 3, "soon": 4}


def _volume_from_cache(symbol: str, *, yahoo_symbol: str | None = None) -> dict[str, Any]:
    """Day volume / relative volume from local daily parquet (no network)."""
    out: dict[str, Any] = {
        "day_volume": None,
        "avg_volume_20": None,
        "rel_volume": None,
        "last": None,
        "change_pct": None,
        "volume_source": "none",
    }
    fetch_sym = yahoo_symbol or symbol
    safe = str(fetch_sym).replace("^", "IDX_")
    if not CACHE_DIR.exists():
        return out
    try:
        import pandas as pd

        candidates = sorted(
            CACHE_DIR.glob(f"{safe}_*_1d.parquet"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        df = None
        for path in candidates:
            try:
                cached = pd.read_parquet(path)
                if cached is not None and not cached.empty and "Volume" in cached.columns:
                    df = cached
                    break
            except Exception:  # noqa: BLE001
                continue
        if df is None or df.empty:
            return out
        last = df.iloc[-1]
        day_vol = float(last.get("Volume") or 0)
        out["day_volume"] = int(day_vol) if day_vol > 0 else None
        out["last"] = float(last.get("Close") or last.get("close") or 0) or None
        if len(df) >= 2 and out["last"]:
            prev = float(df.iloc[-2].get("Close") or df.iloc[-2].get("close") or 0)
            if prev > 0:
                out["change_pct"] = round((out["last"] / prev - 1.0) * 100.0, 2)
        window = df["Volume"].tail(21).astype(float)
        if len(window) >= 6:
            avg = float(window.iloc[:-1].mean()) if len(window) > 1 else float(window.mean())
            out["avg_volume_20"] = int(avg) if avg > 0 else None
            if avg > 0 and day_vol > 0:
                out["rel_volume"] = round(day_vol / avg, 2)
        out["volume_source"] = "cache"
    except Exception as exc:  # noqa: BLE001
        logger.debug("volume cache %s: %s", symbol, exc)
    return out


def _score_map(scores: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in scores or []:
        sym = str(s.get("symbol") or "").upper()
        if not sym:
            continue
        hz = str(s.get("horizon") or "")
        prev = out.get(sym)
        if prev is None or hz == "swing" or (
            prev.get("_hz") != "swing"
            and float(s.get("ensemble_score") or 0) >= float(prev.get("ensemble_score") or 0)
        ):
            row = dict(s)
            row["_hz"] = hz
            out[sym] = row
    return out


def build_market_board(
    *,
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    symbols: list[str] | None = None,
    fetch_earnings: bool = True,
    earnings_max_fetch: int = 60,
    win_table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build earnings + volume leaderboards across the liquid universe."""
    aliases = aliases or {}
    quotes = quotes or {}
    uni = [str(s).upper() for s in (symbols or liquid_universe())]
    # Warm uncached earnings first so the calendar fills across refreshes
    cache_path = Path(__file__).resolve().parents[2] / "outputs" / "earnings_cache.json"
    cached: set[str] = set()
    if cache_path.exists():
        try:
            import json

            raw = json.loads(cache_path.read_text())
            cached = {k for k, v in raw.items() if isinstance(v, dict) and v.get("available")}
        except Exception:  # noqa: BLE001
            cached = set()
    darlings = {s.upper() for s in earnings_darlings_universe()}
    # Prefer darlings + uncached so this week's anticipated names warm first
    uni_sorted = sorted(
        uni,
        key=lambda s: (0 if s in darlings else 1, 0 if s not in cached else 1, s),
    )

    earn_map = earnings_map_for(
        uni_sorted,
        aliases=aliases,
        fetch=fetch_earnings,
        max_fetch=max(0, int(earnings_max_fetch or 0)) if fetch_earnings else 0,
    )
    earnings_watch = scan_earnings_calendar(uni_sorted, aliases=aliases, fetch=False, max_fetch=0)
    smap = _score_map(scores)
    wr_syms = (win_table or {}).get("symbols") or {}

    rows: list[dict[str, Any]] = []
    for sym in uni:
        earn = earn_map.get(sym) or {}
        q = quotes.get(sym) or {}
        sc = smap.get(sym) or {}
        vol = _volume_from_cache(sym, yahoo_symbol=aliases.get(sym))
        last = None
        if q.get("last") is not None:
            last = float(q["last"])
        elif sc.get("last_price") is not None:
            last = float(sc["last_price"])
        elif vol.get("last") is not None:
            last = float(vol["last"])
        change_pct = q.get("change_pct")
        if change_pct is None:
            change_pct = vol.get("change_pct")
        wr = wr_syms.get(sym) or {}
        swing_wr = (wr.get("swing") or {}) if isinstance(wr, dict) else {}
        rows.append(
            {
                "symbol": sym,
                "tier": market_cap_tier(sym),
                "last": last,
                "change_pct": change_pct,
                "day_volume": vol.get("day_volume"),
                "avg_volume_20": vol.get("avg_volume_20"),
                "rel_volume": vol.get("rel_volume"),
                "volume_source": vol.get("volume_source"),
                "swing_score": sc.get("ensemble_score"),
                "quality": bool(sc.get("quality")),
                "horizon": sc.get("_hz") or sc.get("horizon"),
                "earnings_window": earn.get("window") or "none",
                "bucket": earn.get("bucket") or "none",
                "earnings_label": earn.get("label"),
                "next_earnings": earn.get("next_earnings"),
                "last_earnings": earn.get("last_earnings"),
                "days_to_earnings": earn.get("days_to_earnings"),
                "days_since_earnings": earn.get("days_since_earnings"),
                "strategy_bias": earn.get("strategy_bias"),
                "prefer_leap": bool(earn.get("prefer_leap")),
                "hist_swing_win": swing_wr.get("win_pct"),
                "hist_swing_n": swing_wr.get("trades"),
                "in_focus": sym in set(FOCUS_DEFAULT),
                "darling": bool(earn.get("darling")) or sym in darlings,
                "earnings_session": earn.get("earnings_session"),
                "company_name": earn.get("company_name"),
            }
        )

    by_earnings = [
        r
        for r in rows
        if r.get("bucket") in BUCKET_RANK or r.get("earnings_window")
        in {"earnings_day", "pre_earnings", "post_earnings", "earnings_soon"}
    ]
    by_earnings.sort(
        key=lambda r: (
            BUCKET_RANK.get(str(r.get("bucket")), 9),
            0 if r.get("darling") else 1,
            int(r.get("days_to_earnings") if r.get("days_to_earnings") is not None else 999),
            int(r.get("days_since_earnings") if r.get("days_since_earnings") is not None else 999),
            r.get("symbol") or "",
        )
    )
    by_volume = sorted(
        [r for r in rows if r.get("day_volume")],
        key=lambda r: (
            -(float(r.get("rel_volume") or 0)),
            -(int(r.get("day_volume") or 0)),
            r.get("symbol") or "",
        ),
    )
    by_score = sorted(
        [r for r in rows if r.get("swing_score") is not None],
        key=lambda r: (-float(r.get("swing_score") or 0), r.get("symbol") or ""),
    )

    earn_cached = sum(1 for s in uni if s in cached or (earn_map.get(s) or {}).get("window") != "none")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(uni),
        "earnings_cached": len(cached),
        "earnings_classified": sum(
            1 for r in rows if (r.get("earnings_window") or "none") != "none"
        ),
        "volume_cached": sum(1 for r in rows if r.get("day_volume") is not None),
        "note": (
            "Not a full US market scan — Yahoo rate limits force a curated liquid optionable "
            "universe. Earnings cache warms ~"
            f"{int(earnings_max_fetch or 0)} names per refresh until coverage is complete. "
            "Volume uses local daily history cache (rel vol = day / 20d avg)."
        ),
        "by_earnings": by_earnings[:80],
        "by_volume": by_volume[:80],
        "by_score": by_score[:80],
        "earnings_watch": earnings_watch[:60],
        "counts": {
            "today": sum(1 for r in by_earnings if r.get("bucket") == "today"),
            "this_week": sum(1 for r in by_earnings if r.get("bucket") == "this_week"),
            "next_week": sum(1 for r in by_earnings if r.get("bucket") == "next_week"),
            "post": sum(1 for r in by_earnings if r.get("bucket") == "post"),
            "soon": sum(1 for r in by_earnings if r.get("bucket") == "soon"),
            "volume_leaders": len(by_volume),
        },
    }
