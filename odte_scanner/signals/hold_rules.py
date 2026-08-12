"""Industry-style hold / time-stop rules for options desks.

Practices mirrored (educational, not affiliation):
  • 0DTE / same-day: hard flatten into the close (~15:45 ET) — gamma/pin risk
    after ~15:30; most prop 0DTE books do not invent new risk after ~15:00.
  • Weekly (≤7 DTE): typical hold 1–5 sessions; max ~7 calendar days.
  • Swing: challenge tracker already uses 20–60d windows.

Every ENTER should publish an EXIT plan (TP / SL / clock / soft wall).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Clock flatten for 0DTE (hour + minute as float, e.g. 15.75 = 15:45)
ODTE_FLATTEN_ET_HOUR = 15.75  # 15:45 ET
ODTE_NO_NEW_ENTRIES_ET_HOUR = 15.0  # 15:00 ET

HOLD_DEFAULTS: dict[str, dict[str, Any]] = {
    "0dte": {
        "style": "0dte",
        "min_hold_minutes": 5,
        "ideal_hold_minutes": 45,
        "max_hold_minutes": 360,  # same session
        "flatten_et": "15:45",
        "flatten_et_hour": ODTE_FLATTEN_ET_HOUR,
        "label": "same session · flatten by 15:45 ET",
    },
    "weekly": {
        "style": "weekly",
        "min_days": 1,
        "ideal_days": 3,
        "max_days": 7,
        "label": "1–7d (ideal ~3d)",
    },
    "swing": {
        "style": "swing",
        "min_days": 20,
        "ideal_days": 35,
        "max_days": 60,
        "label": "20–60d (ideal ~35d)",
    },
}


def _et_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def bucket_style(dte_bucket: str | None, dte: int | None = None) -> str:
    b = str(dte_bucket or "").lower()
    if b in {"0dte", "1dte", "lottery", "odte"}:
        return "0dte"
    if b in {"weekly", "1w", "week"}:
        return "weekly"
    if b in {"swing", "leap"}:
        return "swing"
    if dte is not None:
        if int(dte) <= 1:
            return "0dte"
        if int(dte) <= 7:
            return "weekly"
        return "swing"
    return "0dte"


def hold_spec(
    dte_bucket: str | None = None,
    dte: int | None = None,
    *,
    weekly_max_days: int | None = None,
    odte_flatten_et: str | None = None,
) -> dict[str, Any]:
    style = bucket_style(dte_bucket, dte)
    spec = dict(HOLD_DEFAULTS[style])
    if style == "weekly" and weekly_max_days is not None:
        spec["max_days"] = int(weekly_max_days)
        spec["label"] = f"1–{spec['max_days']}d (ideal ~{spec['ideal_days']}d)"
    if style == "0dte" and odte_flatten_et:
        spec["flatten_et"] = odte_flatten_et
        try:
            hh, mm = odte_flatten_et.split(":")
            spec["flatten_et_hour"] = int(hh) + int(mm) / 60.0
        except Exception:  # noqa: BLE001
            pass
    return spec


def et_session_hour(now: datetime | None = None) -> float:
    n = _et_now(now)
    return n.hour + n.minute / 60.0 + n.second / 3600.0


def is_weekend_et(now: datetime | None = None) -> bool:
    return _et_now(now).weekday() >= 5


def past_odte_flatten(now: datetime | None = None, flatten_et_hour: float = ODTE_FLATTEN_ET_HOUR) -> bool:
    """True when US equity session should hard-flatten 0DTE risk."""
    n = _et_now(now)
    if n.weekday() >= 5:
        return False
    h = et_session_hour(n)
    return h >= float(flatten_et_hour)


def past_no_new_0dte_entries(now: datetime | None = None) -> bool:
    n = _et_now(now)
    if n.weekday() >= 5:
        return True
    return et_session_hour(n) >= ODTE_NO_NEW_ENTRIES_ET_HOUR


def days_held(entered_at: str | None, now: datetime | None = None) -> float | None:
    if not entered_at:
        return None
    try:
        t0 = datetime.fromisoformat(str(entered_at).replace("Z", "+00:00"))
        t1 = _et_now(now)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=ET)
        return max(0.0, (t1 - t0.astimezone(ET)).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001
        return None


def minutes_held(entered_at: str | None, now: datetime | None = None) -> float | None:
    d = days_held(entered_at, now)
    return None if d is None else round(d * 1440.0, 1)


def time_stop_reason(
    trade: dict[str, Any],
    *,
    now: datetime | None = None,
    weekly_max_days: int = 7,
    odte_flatten_et: str = "15:45",
) -> str | None:
    """Return a SELL reason if industry clock / max-hold says exit, else None."""
    style = bucket_style(trade.get("dte_bucket"), trade.get("dte"))
    spec = hold_spec(
        trade.get("dte_bucket"),
        trade.get("dte"),
        weekly_max_days=weekly_max_days,
        odte_flatten_et=odte_flatten_et,
    )
    entered = trade.get("entered_at") or trade.get("recommended_at")

    if style == "0dte":
        flat_h = float(spec.get("flatten_et_hour") or ODTE_FLATTEN_ET_HOUR)
        if past_odte_flatten(now, flat_h):
            return (
                f"0DTE time-stop — flatten by {spec.get('flatten_et', '15:45')} ET "
                "(desk practice: no gamma into the close)"
            )
        # Weekend / after Friday close with open 0DTE leftover
        n = _et_now(now)
        if n.weekday() >= 5 and entered:
            return "0DTE weekend — force close leftover same-day risk"
        return None

    days = days_held(entered, now)
    if days is None:
        return None
    max_d = int(spec.get("max_days") or weekly_max_days)
    if days >= max_d:
        return f"{style} max hold {max_d}d reached ({days:.1f}d) — roll or exit"
    return None


def exit_plan_text(
    *,
    dte_bucket: str | None,
    dte: int | None = None,
    right: str = "C",
    take_profit_pct: float = 80.0,
    stop_loss_pct: float = 50.0,
    weekly_max_days: int = 7,
    odte_flatten_et: str = "15:45",
    soft_exit: float | None = None,
) -> str:
    """Short EXIT plan string attached to every ENTER / BUY NOW."""
    style = bucket_style(dte_bucket, dte)
    spec = hold_spec(
        dte_bucket,
        dte,
        weekly_max_days=weekly_max_days,
        odte_flatten_et=odte_flatten_et,
    )
    side = "put" if str(right).upper() == "P" else "call"
    bits = [
        f"EXIT plan ({side})",
        f"TP +{take_profit_pct:.0f}%",
        f"SL −{abs(stop_loss_pct):.0f}%",
    ]
    if style == "0dte":
        bits.append(f"clock flatten {spec.get('flatten_et', '15:45')} ET")
        bits.append(f"hold {spec.get('label')}")
    else:
        bits.append(f"hold {spec.get('label')}")
        bits.append(f"max {spec.get('max_days')}d")
    if soft_exit is not None:
        wall = "≤" if str(right).upper() == "P" else "≥"
        bits.append(f"soft wall spot {wall} ${float(soft_exit):.2f}")
    return " · ".join(bits)
