"""Expectancy, Wilson intervals, and the bad-spot filter. No I/O."""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# Two-sided 95% Student-t critical values, indexed by sample size.
_T95 = {
    2: 12.706,
    3: 4.303,
    4: 3.182,
    5: 2.776,
    6: 2.571,
    7: 2.447,
    8: 2.365,
    9: 2.306,
    10: 2.262,
    11: 2.228,
    12: 2.201,
    13: 2.179,
    14: 2.160,
    15: 2.145,
    16: 2.131,
    17: 2.120,
    18: 2.110,
    19: 2.101,
    20: 2.093,
    21: 2.086,
    22: 2.080,
    23: 2.074,
    24: 2.069,
    25: 2.064,
    26: 2.060,
    27: 2.056,
    28: 2.052,
    29: 2.048,
    30: 2.045,
}

# The Oct 2026 tape stored an ensemble score near $62–$67 as the underlying
# for these names while the listed strikes were $150–$430.
NAMED_BAD_SPOTS = frozenset({"AMAT", "ALAB", "CRM", "NOW"})


def t_crit(n: int) -> float:
    if n < 2:
        return float("inf")
    if n in _T95:
        return _T95[n]
    if n < 40:
        return 2.03
    if n < 60:
        return 2.009
    if n < 120:
        return 1.98
    return 1.96


def mean_interval(values: list[float]) -> dict[str, float | int | None]:
    """Sample mean and a 95% t interval. One observation has no interval."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    mu = float(mean(values))
    if n == 1:
        return {"n": 1, "mean": mu, "lo": None, "hi": None}
    var = sum((x - mu) ** 2 for x in values) / (n - 1)
    se = math.sqrt(var / n)
    half = t_crit(n) * se
    return {"n": n, "mean": mu, "lo": mu - half, "hi": mu + half}


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return max(0.0, centre - margin), min(1.0, centre + margin)


def parse_occ(contract: str | None) -> tuple[datetime | None, str | None, float | None]:
    text = str(contract or "").upper()
    if len(text) < 15:
        return None, None, None
    right = text[-9]
    try:
        strike = int(text[-8:]) / 1000.0
    except ValueError:
        return None, None, None
    try:
        expiry = datetime(2000 + int(text[-15:-13]), int(text[-13:-11]), int(text[-11:-9]))
    except ValueError:
        expiry = None
    if right not in {"C", "P"} or strike <= 0:
        return None, None, None
    return expiry, right, strike


def bad_spot_reasons(row: dict[str, Any]) -> list[str]:
    """Drop a scorecard row whose reference price cannot be the underlying."""
    ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
    try:
        ref = float(row.get("reference_entry"))
    except (TypeError, ValueError):
        return []
    _expiry, _right, strike = parse_occ(str(row.get("contract") or ""))
    if strike is None or strike <= 0 or ref <= 0:
        return []
    reasons: list[str] = []
    if ticker in NAMED_BAD_SPOTS and ref < strike * 0.55:
        reasons.append("named_bad_spot")
    if ref < strike * 0.45 or ref > strike * 2.2:
        reasons.append("spot_vs_strike")
    return reasons


def _round(value: float | None, places: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), places)


def summarize_pairs(pairs: list[tuple[str, float]]) -> dict[str, Any]:
    """Win rate, average win and loss, expectancy, and the second-half mean.

    ``pairs`` are ``(time, value)`` already in the unit being reported.
    A win is a value above zero. The second half is the later half after
    sorting by time, and it is the out-of-sample check.
    """
    ordered = sorted(pairs, key=lambda item: item[0])
    values = [value for _at, value in ordered]
    n = len(values)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    stat = mean_interval(values)
    win_lo, win_hi = wilson_interval(len(wins), n)
    mid = n // 2
    h2 = [value for _at, value in ordered[mid:]]
    h2_mean = float(mean(h2)) if h2 else None
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _round((len(wins) / n) if n else None, 4),
        "win_lo": _round(win_lo, 4),
        "win_hi": _round(win_hi, 4),
        "avg_win": _round(float(mean(wins)) if wins else None, 4),
        "avg_loss": _round(float(mean(losses)) if losses else None, 4),
        "expectancy": _round(stat["mean"] if isinstance(stat["mean"], float) else None, 4),
        "lo": _round(stat["lo"] if isinstance(stat["lo"], float) else None, 4),
        "hi": _round(stat["hi"] if isinstance(stat["hi"], float) else None, 4),
        "h2_n": len(h2),
        "h2_expectancy": _round(h2_mean, 4),
    }


def clears_bar(summary: dict[str, Any], *, min_samples: int) -> bool:
    """Positive expectancy only counts with enough trades and a positive second half."""
    n = int(summary.get("n") or 0)
    lo = summary.get("lo")
    h2 = summary.get("h2_expectancy")
    if n < min_samples or not isinstance(lo, (int, float)) or not isinstance(h2, (int, float)):
        return False
    return float(lo) > 0 and float(h2) > 0


def chicago_hour(iso: str | None) -> int | None:
    raw = str(iso or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CT)
    return dt.astimezone(CT).hour


def tod_bucket(iso: str | None) -> str:
    hour = chicago_hour(iso)
    if hour is None:
        return "unknown"
    if hour < 8:
        return "before 8 CT"
    if hour < 10:
        return "8–10 CT"
    if hour < 12:
        return "10–12 CT"
    if hour < 14:
        return "12–2 CT"
    return "after 2 CT"


def dte_bucket(row: dict[str, Any]) -> str:
    expiry, _right, _strike = parse_occ(str(row.get("contract") or ""))
    raw = str(row.get("signaled_at") or "")
    try:
        day = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return "unknown"
    if expiry is None:
        return "unknown"
    dte = (expiry.date() - day).days
    if dte <= 0:
        return "0 DTE"
    if dte == 1:
        return "1 DTE"
    if dte <= 7:
        return "2–7 DTE"
    if dte <= 21:
        return "8–21 DTE"
    return "22+ DTE"


def moneyness_bucket(row: dict[str, Any]) -> str:
    try:
        ref = float(row.get("reference_entry"))
    except (TypeError, ValueError):
        return "unknown"
    _expiry, _right, strike = parse_occ(str(row.get("contract") or ""))
    if strike is None or ref <= 0:
        return "unknown"
    dist = abs(ref - strike) / ref
    if dist < 0.01:
        return "ATM <1%"
    if dist < 0.03:
        return "1–3%"
    if dist < 0.08:
        return "3–8%"
    return ">8%"
