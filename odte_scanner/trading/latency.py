"""Order latency per desk: signal → staged → sent → acked → filled.

Stamps (all ISO UTC on the ledger row):
- ``meta.detected_at``  when the desk saw the signal (Sniper/Diamond/Afternoon)
- ``meta.signal_at_ct`` the signal bar ("HH:MM" Chicago)
- ``created_at``        staged in the ledger
- ``meta.first_sent_at`` first HTTP attempt to Webull (``sent_at`` = last attempt)
- ``meta.acked_at``     Webull returned an order id
- ``meta.wb_place_time_at`` / ``meta.wb_filled_time_at`` Webull's own times
  (order history) — preferred for sent and filled when present
- ``meta.filled_at``    when this app first saw the fill

Rows without a stamp are skipped for that stage, never guessed.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

STAGES = (
    "signal_bar→sent_s",
    "bar_close→sent_s",
    "signal_bar→detected_s",
    "detected→cycle_s",
    "cycle→boards_s",
    "boards→route_s",
    "route→quote_s",
    "quote→staged_s",
    "detected→staged_s",
    "staged→sent_s",
    "sent→acked_s",
    "sent→filled_s",
)
BAR_SEC = 300  # Sniper, Diamond and Afternoon Puts use 5-minute bars stamped at the bar start

# Per-stage meaning (signal side, buys only):
#   signal_bar→detected  bar start -> first cycle whose board showed the signal.
#                        Sniper/Diamond read the forming bar, so this is mostly
#                        how long the bar took to meet the rule, not lag.
#   bar_close→sent       sent minus (bar start + 5 min). Negative = sent before
#                        the bar closed (forming-bar desks).
#   detected→cycle       first sighting -> start of the cycle that routed it
#                        (0 when routed on first sight; >0 = gated or budgeted).
#   cycle→boards         bars + boards + option chains for that cycle.
#   boards→route         routing other rows / chases before this one.
#   route→quote          live quote fetch for this contract.
#   quote→staged         gates, sizing, ledger write.


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta")
    return meta if isinstance(meta, dict) else {}


def _bar_time(row: dict[str, Any], anchor: datetime | None) -> datetime | None:
    """Signal bar as a datetime: ``signal_at_ct`` or 'signal HH:MM CT' in the reason."""
    if anchor is None:
        return None
    text = str(_meta(row).get("signal_at_ct") or "")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not match:
        found = re.search(r"signal (\d{1,2}):(\d{2}) CT", str(row.get("reason") or ""))
        match = found
    if not match:
        return None
    day = anchor.astimezone(CT).date()
    return datetime(day.year, day.month, day.day, int(match[1]), int(match[2]), tzinfo=CT)


def desk_key(row: dict[str, Any]) -> str:
    meta = _meta(row)
    action = str(row.get("action") or "").upper()
    if action == "SELL":
        tier = str(meta.get("exit_tier") or meta.get("golden_kind") or "").lower()
        tier = {"sure_winner_gold": "gold", "sure_winner": "gold", "afternoon_puts": "afternoon"}.get(tier, tier)
        if str(row.get("desk") or "") == "expiry_close":
            return "expiry"
        return f"exit:{tier or 'gold'}"
    kind = str(meta.get("golden_kind") or row.get("desk") or "other").lower()
    return {"sure_winner_gold": "gold", "sure_winner": "gold"}.get(kind, kind)


def order_latency(row: dict[str, Any]) -> dict[str, float]:
    """Seconds for each stage this row has stamps for."""
    meta = _meta(row)
    out: dict[str, float] = {}
    staged = _dt(row.get("created_at"))
    sent = _dt(meta.get("wb_place_time_at")) or _dt(meta.get("first_sent_at")) or _dt(meta.get("sent_at"))
    acked = _dt(meta.get("acked_at"))
    filled = _dt(meta.get("wb_filled_time_at")) or _dt(meta.get("filled_at"))
    detected = _dt(meta.get("detected_at"))
    restored = meta.get("source") == "webull_history_import"  # created_at is Webull's place time
    if staged and sent and not restored and (sent - staged) >= timedelta(seconds=-5):
        out["staged→sent_s"] = (sent - staged).total_seconds()
    attempt = _dt(meta.get("sent_at"))
    if attempt and acked and acked >= attempt:
        out["sent→acked_s"] = (acked - attempt).total_seconds()
    if sent and filled and filled >= sent:
        out["sent→filled_s"] = (filled - sent).total_seconds()
    if detected and staged and staged >= detected:
        out["detected→staged_s"] = (staged - detected).total_seconds()
    # A chase replacement is a reprice of an entry already sent: signal-side
    # stages belong to the first order only.
    if str(row.get("action") or "").upper() == "BUY" and not meta.get("replaces"):
        bar = _bar_time(row, staged)
        if bar and sent and sent >= bar:
            out["signal_bar→sent_s"] = (sent - bar).total_seconds()
            out["bar_close→sent_s"] = (sent - bar).total_seconds() - BAR_SEC
        if bar and detected and detected >= bar:
            out["signal_bar→detected_s"] = (detected - bar).total_seconds()
        chain = [
            ("detected→cycle_s", detected, _dt(meta.get("cycle_started_at"))),
            ("cycle→boards_s", _dt(meta.get("cycle_started_at")), _dt(meta.get("boards_ready_at"))),
            ("boards→route_s", _dt(meta.get("boards_ready_at")), _dt(meta.get("route_started_at"))),
            ("route→quote_s", _dt(meta.get("route_started_at")), _dt(meta.get("quote_ready_at"))),
            ("quote→staged_s", _dt(meta.get("quote_ready_at")), staged),
        ]
        for name, start, end in chain:
            # Clock-skew guard only; the stamps are one process's wall clock.
            if start and end and (end - start) >= timedelta(seconds=-1):
                out[name] = max(0.0, (end - start).total_seconds())
    return out


def _q(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def latency_report(orders: list[dict[str, Any]] | None, *, since: datetime | None = None) -> dict[str, Any]:
    """Median / p90 per desk and stage for orders that reached Webull."""
    buckets: dict[str, dict[str, list[float]]] = {}
    for row in orders or []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() == "SPX":
            continue
        if row.get("status") in {"dry_run", "skipped"}:
            continue
        staged = _dt(row.get("created_at"))
        if since is not None and (staged is None or staged < since):
            continue
        stages = order_latency(row)
        if not stages:
            continue
        desk = buckets.setdefault(desk_key(row), {})
        for name, value in stages.items():
            desk.setdefault(name, []).append(value)
    out: dict[str, Any] = {}
    for desk, stages in sorted(buckets.items()):
        out[desk] = {
            name: {"n": len(vals), "median": _q(vals, 0.5), "p90": _q(vals, 0.9), "max": round(max(vals), 1)}
            for name, vals in stages.items()
        }
    return {"desks": out, "since": since.isoformat() if since else None, "stages": list(STAGES)}
