"""Skip rules for alerts and paper, with the evidence on the card.

These are filters, not a mute. A skipped Golden Flow print stays on the board
with ``edge_skip`` set. Telegram does not send it. Golden Flow is not a Webull
route. Gold Setup is not hard-dropped: 1 DTE and 12:00–14:00 CT only sort ahead
of the same confidence tier.

Every number on the card is the in-sample underlying move Nikita cited from
the live host (bad spots removed). A win is the underlying moving the signal's
way. It is not an option fill.

On the public scorecard at 2026-10-27T03:45:04Z the same rules, applied
together, take Golden Flow from n=338, expectancy −0.28% [−0.49, −0.07]
to n=143, +0.06% [−0.18, +0.29]. The later half of what remains is +0.22%.
The interval still includes zero, so the sample did not clear the bar.
A better in-sample average is not proof the next trade wins.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.edge.metrics import chicago_hour, parse_occ

CT = ZoneInfo("America/Chicago")

# Live host /api/edge at 2026-10-27T03:21Z. Public pull was a few rows larger
# and the same sign. Intervals are 95% t intervals on the underlying percent.
GF_PUT = (
    "Skipped: Golden Flow puts/shorts, n=88, underlying expectancy −0.71% "
    "[−1.07, −0.34]. In-sample."
)
GF_NEAR = (
    "Skipped: Golden Flow 1–3% from the money, n=133, −0.62% [−1.06, −0.19]. In-sample."
)
GF_DTE = "Skipped: Golden Flow 8–21 DTE, n=16, −0.92% [−1.81, −0.03]. In-sample."
GF_OPEN = (
    "Skipped: Golden Flow before 8:00 CT, n=14, −0.99% [−2.86, +0.87]. In-sample."
)
GF_MID = "Skipped: Golden Flow 10–12 CT, n=39, −0.53% [−0.97, −0.09]. In-sample."

# Soft preference only. Both intervals include zero. n is too small to drop
# the other Gold Setup cards.
GOLD_FAVOR_NOTE = (
    "Gold Setup 1 DTE (n=14, +0.36% [−0.81, +1.53]) and 12–2 CT "
    "(n=12, +0.57% [−0.91, +2.05]) sort ahead of the same tier. Not a hard filter."
)


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _right(row: dict[str, Any]) -> str | None:
    right = str(row.get("right") or "").upper()
    if right in {"P", "PUT"}:
        return "P"
    if right in {"C", "CALL"}:
        return "C"
    _expiry, parsed, _strike = parse_occ(str(row.get("contract") or ""))
    return parsed


def _is_short(row: dict[str, Any]) -> bool:
    if _right(row) == "P":
        return True
    direction = str(row.get("direction") or "").strip().lower()
    return direction in {"short", "put", "bear"}


def _distance(row: dict[str, Any]) -> float | None:
    """Absolute distance from the money, as a fraction of spot. 0.02 is 2%."""
    moneyness = _as_float(row.get("moneyness_pct"))
    if moneyness is not None:
        return abs(moneyness) / 100.0
    ref = _as_float(row.get("reference_entry"))
    if ref is None or ref <= 0:
        ref = _as_float(row.get("spot"))
    strike = _as_float(row.get("strike"))
    if strike is None or strike <= 0:
        _expiry, _right_parsed, parsed = parse_occ(str(row.get("contract") or ""))
        strike = parsed
    if ref is None or ref <= 0 or strike is None or strike <= 0:
        return None
    return abs(ref - strike) / ref


def _calendar_dte(row: dict[str, Any]) -> int | None:
    """Calendar days from the signal date to expiry, matching the audit buckets."""
    expiry, _right_parsed, _strike = parse_occ(str(row.get("contract") or ""))
    raw = str(row.get("signaled_at") or "").strip()
    if expiry is not None and raw:
        try:
            day = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            day = None
        if day is not None:
            return (expiry.date() - day).days
    explicit = row.get("dte")
    if explicit is None or explicit == "":
        return None
    try:
        return int(explicit)
    except (TypeError, ValueError):
        return None


def _chicago_hour(row: dict[str, Any], now: datetime | None) -> int | None:
    for key in ("signaled_at", "time", "ts"):
        hour = chicago_hour(str(row.get(key) or ""))
        if hour is not None:
            return hour
    if now is None:
        return None
    moment = now if now.tzinfo is not None else now.replace(tzinfo=CT)
    return moment.astimezone(CT).hour


def golden_flow_skip_reason(row: dict[str, Any], now: datetime | None = None) -> str | None:
    """Return the card reason, or None when the print may be alerted.

    Order is the order Nikita named. A put that is also 2% from the money
    shows the put reason. The clock is America/Chicago. "Before 8:00 CT" is
    the measured bucket (hour < 8). The first half hour after the 8:30 CT
    open is not this bucket and is not cut.
    """
    if not isinstance(row, dict):
        return None
    if _is_short(row):
        return GF_PUT
    distance = _distance(row)
    if distance is not None and 0.01 <= distance < 0.03:
        return GF_NEAR
    dte = _calendar_dte(row)
    if dte is not None and 8 <= dte <= 21:
        return GF_DTE
    hour = _chicago_hour(row, now)
    if hour is not None and hour < 8:
        return GF_OPEN
    if hour is not None and 10 <= hour < 12:
        return GF_MID
    return None


def stamp_flow_prints(prints: list[dict[str, Any]], now: datetime | None = None) -> None:
    """Set ``edge_skip`` on each print. Clearing a stale reason is intentional."""
    for row in prints:
        if not isinstance(row, dict):
            continue
        reason = golden_flow_skip_reason(row, now)
        if reason:
            row["edge_skip"] = reason
        else:
            row.pop("edge_skip", None)


def gold_setup_preferred(row: dict[str, Any], now: datetime | None = None) -> bool:
    """True when this Gold Setup card is in a slice that leaned positive.

    Either condition is enough. Requiring both would be a smaller, unmeasured
    intersection. This does not drop the other cards.
    """
    if not isinstance(row, dict):
        return False
    dte = _calendar_dte(row)
    if dte == 1:
        return True
    hour = _chicago_hour(row, now)
    return hour is not None and 12 <= hour < 14
