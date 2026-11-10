"""Go-live checks for the paper exits, from the Webull paper ledger.

Every number comes from ledger rows (Webull paper fills are the source of
truth once the history reconcile has run). Thresholds in ``checks`` are
suggested defaults, not tuned values.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.trading.latency import _dt, _q

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

STAGED_STUCK_SEC = 20
WORKING_STUCK_SEC = 600
DEFAULT_STOP = {"sniper": 40.0, "diamond": 40.0, "afternoon": 40.0, "gold": 50.0}
OVERSHOOT_PTS = 10.0


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta")
    return meta if isinstance(meta, dict) else {}


def stuck_sells(orders: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in orders:
        if str(row.get("action") or "").upper() != "SELL" or str(row.get("symbol") or "").upper() == "SPX":
            continue
        status = str(row.get("status") or "")
        created = _dt(row.get("created_at"))
        if created is None:
            continue
        age = (now - created).total_seconds()
        why = None
        if status == "staged" and age > STAGED_STUCK_SEC:
            why = "staged, never confirmed sent"
        elif status == "submitted" and age > WORKING_STUCK_SEC:
            why = "working over 10 min"
        elif status == "deferred" and not _meta(row).get("queued_for_open") and age > 120:
            why = "deferred (rate limit) over 2 min"
        if why and age < 7 * 86400:
            out.append(
                {
                    "id": row.get("id"),
                    "contract": row.get("contract"),
                    "status": status,
                    "age_min": round(age / 60.0, 1),
                    "why": why,
                }
            )
    return out


def _buy_for(trip: dict[str, Any], by_id: dict[str, dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, Any] | None:
    sell = by_id.get(str(trip.get("order_id") or "")) or {}
    closes = str(_meta(sell).get("closes_order_id") or "")
    if closes and closes in by_id:
        return by_id[closes]
    # Fallback: the latest buy of this contract placed before the exit.
    contract = trip.get("contract")
    exited = _dt(trip.get("exited_at"))
    best: dict[str, Any] | None = None
    best_at: datetime | None = None
    for row in orders:
        if str(row.get("action") or "").upper() != "BUY" or row.get("contract") != contract:
            continue
        at = _dt(row.get("created_at"))
        if exited is not None and at is not None and at > exited:
            continue
        if best is None or (at is not None and (best_at is None or at > best_at)):
            best, best_at = row, at
    return best


def loss_vs_planned_stop(trips: list[dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(o.get("id")): o for o in orders}
    rows: list[dict[str, Any]] = []
    # Losses per lot: a scale-out sells one lot in pieces.
    lots: dict[str, dict[str, Any]] = {}
    for trip in trips:
        entry = float(trip.get("entry") or 0)
        if entry <= 0:
            continue
        key = str(trip.get("contract") or "") + "|" + str(trip.get("entered_at") or "")
        lot = lots.setdefault(key, {"trip": trip, "cost": 0.0, "proceeds": 0.0})
        qty = int(trip.get("contracts") or 1)
        lot["cost"] += entry * qty
        lot["proceeds"] += float(trip.get("exit") or 0) * qty
        lot["expired"] = bool(trip.get("expired")) or lot.get("expired", False)
    for lot in lots.values():
        trip = lot["trip"]
        if lot["cost"] <= 0 or lot["proceeds"] >= lot["cost"]:
            continue
        realized = (lot["proceeds"] - lot["cost"]) / lot["cost"] * 100.0
        buy = _buy_for(trip, by_id, orders)
        planned = None
        if buy is not None:
            try:
                planned = float(_meta(buy).get("planned_stop_pct"))
            except (TypeError, ValueError):
                planned = None
        source = "stamped"
        if planned is None:
            planned = DEFAULT_STOP.get(str(trip.get("tier") or ""), 50.0)
            source = "desk default"
        overshoot = -realized - planned
        rows.append(
            {
                "contract": trip.get("contract"),
                "desk": trip.get("desk"),
                "realized_pct": round(realized, 1),
                "planned_stop_pct": -planned,
                "planned_from": source,
                "overshoot_pts": round(overshoot, 1),
                "expired": bool(lot.get("expired")),
                "pnl": round((lot["proceeds"] - lot["cost"]) * 100.0, 2),
            }
        )
    beyond = [r for r in rows if r["overshoot_pts"] > OVERSHOOT_PTS]
    beyond.sort(key=lambda r: r["overshoot_pts"], reverse=True)
    return {
        "losing_lots": len(rows),
        "beyond_stop": len(beyond),
        "tolerance_pts": OVERSHOOT_PTS,
        "worst": beyond[:10],
    }


_DEADLINE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(pm)?\s*(ct|et)", re.IGNORECASE)


def flatten_lateness(orders: list[dict[str, Any]], since: datetime) -> dict[str, Any]:
    late: list[float] = []
    rows: list[dict[str, Any]] = []
    for row in orders:
        if str(row.get("action") or "").upper() != "SELL":
            continue
        reason = str(row.get("reason") or "")
        if "flatten" not in reason.lower() and "time-stop" not in reason.lower():
            continue
        meta = _meta(row)
        sent = _dt(meta.get("wb_place_time_at")) or _dt(meta.get("first_sent_at")) or _dt(meta.get("sent_at"))
        if sent is None or sent < since:
            continue
        match = _DEADLINE_RE.search(reason)
        if not match:
            continue
        hour = int(match[1])
        if match[3] and hour < 12:
            hour += 12
        zone = CT if match[4].lower() == "ct" else ET
        day = sent.astimezone(zone).date()
        due = datetime(day.year, day.month, day.day, hour, int(match[2]), tzinfo=zone)
        seconds = (sent - due).total_seconds()
        if seconds < -120 or seconds > 6 * 3600:
            continue  # a retry hours later is reported under stuck sells, not here
        late.append(seconds)
        rows.append({"contract": row.get("contract"), "sent_after_deadline_s": round(seconds), "status": row.get("status")})
    rows.sort(key=lambda r: r["sent_after_deadline_s"], reverse=True)
    return {
        "n": len(late),
        "median_s": _q(late, 0.5),
        "p90_s": _q(late, 0.9),
        "max_s": round(max(late)) if late else None,
        "within_60s": sum(1 for v in late if v <= 60),
        "latest": rows[:10],
    }


def _profit_factor(trips: list[dict[str, Any]]) -> float | None:
    wins = sum(float(t["pnl"]) for t in trips if float(t.get("pnl") or 0) > 0)
    losses = -sum(float(t["pnl"]) for t in trips if float(t.get("pnl") or 0) < 0)
    if losses <= 0:
        # No losing trade yet: undefined. Never inf — it is not valid JSON.
        return None
    return round(wins / losses, 2)


def _drawdown(trips: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trips, key=lambda t: str(t.get("exited_at") or ""))
    equity = peak = 0.0
    worst = 0.0
    for trip in ordered:
        equity += float(trip.get("pnl") or 0)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return {"max_drawdown_usd": round(worst, 2), "realized_usd": round(equity, 2)}


def build_exit_health(
    orders: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    days: int = 7,
    equity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from odte_scanner.trading.portal_book import round_trips

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    book = [o for o in (orders or []) if isinstance(o, dict)]
    since = moment - timedelta(days=days)
    trips_all = round_trips(book)
    trips = [t for t in trips_all if (_dt(t.get("exited_at")) or moment) >= since]
    stuck = stuck_sells(book, moment)
    stops = loss_vs_planned_stop(trips, book)
    flats = flatten_lateness(book, since)
    pf = _profit_factor(trips)
    pf_all = _profit_factor(trips_all)
    dd = _drawdown(trips_all)
    sessions = sorted({(_dt(t.get("exited_at")) or moment).astimezone(CT).date().isoformat() for t in trips_all})
    expired = [t for t in trips if t.get("expired")]
    drawdown = dict(dd)
    if equity:
        drawdown["webull_equity"] = equity
        drawdown["source"] = "realized paper P&L curve; Webull equity shown is the current balance only"
    else:
        drawdown["source"] = "realized paper P&L curve (Webull equity history not available)"
    pf_value = pf
    checks = {
        "no_stuck_sells": {"ok": not stuck, "value": len(stuck)},
        "losses_within_planned_stop": {"ok": stops["beyond_stop"] == 0, "value": stops["beyond_stop"]},
        "flatten_p90_within_60s": {
            "ok": flats["p90_s"] is not None and flats["p90_s"] <= 60,
            "value": flats["p90_s"],
        },
        "profit_factor_min_1.2": {
            "ok": (pf_value >= 1.2) if pf_value is not None else any(float(t.get("pnl") or 0) > 0 for t in trips),
            "value": pf if pf is not None else ("no losses" if trips else None),
        },
        "sessions_min_20": {"ok": len(sessions) >= 20, "value": len(sessions)},
    }
    return {
        "asof": moment.isoformat(),
        "window_days": days,
        "stuck_sells": {"n": len(stuck), "rows": stuck[:20]},
        "loss_vs_planned_stop": stops,
        "flatten_time": flats,
        "profit_factor": {
            "window": pf,
            "all": pf_all,
            "includes_expired": True,
            "expired_lots_in_window": len(expired),
            "trades_in_window": len(trips),
        },
        "drawdown": drawdown,
        "sessions": {"n": len(sessions), "first": sessions[0] if sessions else None, "last": sessions[-1] if sessions else None},
        "checks": checks,
        "checks_note": "Thresholds are suggested defaults for go-live review, not tuned values.",
        "go_live_ready": all(c["ok"] for c in checks.values()),
    }
