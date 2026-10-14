"""Replay one long-option premium path under the exit study's rules.

Bars are ``{t, h, l, c}`` with ``t`` as unix seconds. Decisions use the bar
close. Highs and lows are only the reported excursion, not the fill.

Rules
-----
current, tp30, tp50, tp80, tp100
    Sell when close reaches that premium gain. ``current`` is +80%.
    All of these also sell at −50% from entry.
trail
    Sell at −50%, or after the high is +20% from entry and the close
    gives back 20% from that high.
time
    Sell on the first close at least 45 minutes after entry.
eod
    Mark the last close at or before 16:00 ET on the entry session.
    No premium target and no stop.
next_day
    Mark the last close at or before 16:00 ET on the next weekday.
    A contract that expires on the entry session stops at that 16:00.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RULES = ("current", "tp30", "tp50", "tp80", "tp100", "trail", "time", "eod", "next_day")
_TP = {"current": 0.80, "tp30": 0.30, "tp50": 0.50, "tp80": 0.80, "tp100": 1.00}


def _session_close(day, hour: int = 16, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def _next_weekday(day):
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def replay_exit(
    entry: float,
    bars: list[dict[str, Any]],
    rule: str,
    *,
    entry_ts: datetime,
    expiry_day=None,
    intraday: bool = False,
) -> dict[str, Any] | None:
    """Return the exit fill for one rule, or None when the path is empty."""
    if entry <= 0 or rule not in RULES:
        return None
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.replace(tzinfo=ET)
    entry_unix = int(entry_ts.timestamp())
    path = [b for b in bars if int(b["t"]) >= entry_unix and float(b.get("c") or 0) > 0]
    if not path:
        return None
    entry_day = entry_ts.astimezone(ET).date()
    eod = _session_close(entry_day, 16, 0)
    flatten = _session_close(entry_day, 15, 45)
    nxt = _session_close(_next_weekday(entry_day), 16, 0)
    if expiry_day is not None and expiry_day <= entry_day:
        nxt = eod
    if rule == "eod":
        end = int(eod.timestamp())
        path = [b for b in path if int(b["t"]) <= end]
    elif rule == "next_day":
        end = int(nxt.timestamp())
        path = [b for b in path if int(b["t"]) <= end]
    elif rule == "current" and intraday:
        end = int(flatten.timestamp())
        path = [b for b in path if int(b["t"]) <= end]
    elif rule == "current":
        end = int(nxt.timestamp())
        path = [b for b in path if int(b["t"]) <= end]
    elif rule == "time":
        path = [b for b in path if int(b["t"]) <= entry_unix + 45 * 60]
    elif rule in _TP or rule == "trail":
        end = int(nxt.timestamp())
        path = [b for b in path if int(b["t"]) <= end]
    if not path:
        return None
    highs = [float(b["h"]) for b in path if b.get("h")]
    lows = [float(b["l"]) for b in path if b.get("l") and float(b["l"]) > 0]
    mfe = (max(highs) - entry) / entry * 100.0 if highs else None
    mae = (min(lows) - entry) / entry * 100.0 if lows else None
    peak = entry
    exit_px = float(path[-1]["c"])
    why = "horizon"
    tp = _TP.get(rule)
    for bar in path:
        px = float(bar["c"])
        hi = float(bar["h"]) if bar.get("h") else px
        peak = max(peak, hi)
        ret = (px - entry) / entry
        if rule == "trail":
            if ret <= -0.50:
                exit_px, why = px, "sl"
                break
            if peak >= entry * 1.20 and px <= peak * 0.80:
                exit_px, why = px, "trail"
                break
        elif tp is not None:
            if ret >= tp:
                exit_px, why = px, "tp"
                break
            if ret <= -0.50:
                exit_px, why = px, "sl"
                break
        elif rule == "time" and int(bar["t"]) >= entry_unix + 45 * 60:
            exit_px, why = px, "time"
            break
        elif rule in {"eod", "next_day"}:
            exit_px, why = px, rule
    pnl = (exit_px - entry) * 100.0
    return {
        "exit": exit_px,
        "why": why,
        "pnl": pnl,
        "win": pnl > 1.0,
        "mfe": mfe,
        "mae": mae,
    }
