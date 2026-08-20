"""Assemble the ZeroLoss board: catalyst tape + Bullflow-style flow prints."""

from __future__ import annotations

from typing import Any

import pandas as pd

from odte_scanner.zeroloss.catalyst import (
    LANE_DO_NOT_MISS,
    fetch_yahoo_headlines,
    score_session,
)

DISCLAIMER = (
    "ZeroLoss is a miss-prevention desk, not a promise of zero losses. "
    "No scanner picks only winning stocks. MRNA +177% on a Phase 3 readout is the "
    "same event class that can gap down on a failed trial. Paper research only — "
    "not financial advice. Not affiliated with Bullflow, Unusual Whales, or Signa."
)

# Always keep these on the published board even on a quiet session.
PINNED_SYMBOLS = ("MRNA", "MP", "USAR", "PFE", "BNTX", "XBI", "UUUU", "CCJ", "ALB")


def build_zeroloss_board(
    histories: dict[str, pd.DataFrame],
    *,
    flow: dict[str, Any] | None = None,
    quotes: dict[str, Any] | None = None,
    headlines_by_symbol: dict[str, list[str]] | None = None,
    fetch_news: bool = False,
    max_news: int = 12,
) -> dict[str, Any]:
    headlines_by_symbol = {
        str(k).upper(): list(v) for k, v in (headlines_by_symbol or {}).items()
    }
    keyed: dict[str, pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    for sym, df in histories.items():
        key = str(sym).upper()
        keyed[key] = df
        titles = list(headlines_by_symbol.get(key) or [])
        row = score_session(df, symbol=key, news_titles=titles)
        q = (quotes or {}).get(key) or (quotes or {}).get(sym)
        if isinstance(q, dict):
            live = q.get("session_change_pct", q.get("change_pct"))
            last = q.get("last")
            if last:
                row["live_last"] = last
            if live is not None:
                row["live_change_pct"] = live
        rows.append(row)

    rows.sort(key=lambda r: float(r.get("miss_score") or 0), reverse=True)
    if fetch_news:
        for row in rows[:max_news]:
            key = str(row.get("symbol") or "").upper()
            if headlines_by_symbol.get(key):
                continue
            titles = fetch_yahoo_headlines(key)
            if not titles:
                continue
            headlines_by_symbol[key] = titles
            df = keyed.get(key)
            if df is None:
                continue
            updated = score_session(df, symbol=key, news_titles=titles)
            row.update(updated)
        rows.sort(key=lambda r: float(r.get("miss_score") or 0), reverse=True)
    pinned = [r for r in rows if str(r.get("symbol") or "").upper() in PINNED_SYMBOLS]
    do_not_miss = [r for r in rows if r.get("lane") == LANE_DO_NOT_MISS]
    catalyst = [r for r in rows if r.get("lane") == "CATALYST"]
    tape = [r for r in rows if r.get("lane") == "TAPE"]
    rest = [r for r in rows if str(r.get("symbol") or "").upper() not in PINNED_SYMBOLS]

    prints = list((flow or {}).get("prints") or [])
    prints = sorted(prints, key=lambda p: abs(float(p.get("flow_score") or 0)), reverse=True)[:40]

    counts = {
        "scanned": len(rows),
        "do_not_miss": len(do_not_miss),
        "catalyst": len(catalyst),
        "tape": len(tape),
        "flow_prints": len(prints),
    }
    return {
        "brand": "ZeroLoss",
        "purpose": "Do not miss the tape. Catch gap/volume/news names the hist-win gate hid.",
        "disclaimer": DISCLAIMER,
        "counts": counts,
        "do_not_miss": do_not_miss[:20],
        "catalyst": catalyst[:20],
        "tape": tape[:20],
        "pinned": pinned,
        "all": (pinned + rest)[:80],
        "flow_prints": prints,
        "mrna_note": (
            "MRNA was missing because it was not on the focus or liquid scan lists. "
            "ZeroLoss always includes a biotech/event sleeve (MRNA, BNTX, XBI, …)."
        ),
    }
