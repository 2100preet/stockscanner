"""Afternoon Puts 30-day shadow log. Logging only: it never places an order.

For every Afternoon Puts paper entry it tracks a hypothetical exit on the
same contract and puts it next to the real fills:

* **Shadow exit:** sell the whole position at +150% (a Limit at fill × 2.5).
  The desk's −40% stop and the 2:45 PM CT flatten stay the same. Whichever
  live-quote event comes first closes the shadow lot.
* **After-noon tag:** the entry filled at or after 12:00 PM CT.

Prices come from live contract quotes (Tradier, then Polygon, via
``fetch_contract_quote``) polled from the fast exit loop. Real exits come from
our own filled sells in the Webull paper ledger.

Store: ``outputs/afternoon_puts_shadow.json`` on the Fly volume
(``AFTERNOON_PUTS_SHADOW_PATH`` overrides). It runs for 30 days from the first
day it logs (``AFTERNOON_PUTS_SHADOW_START`` = YYYY-MM-DD overrides), then stops
polling on its own and keeps the log readable. Kill switch:
``AFTERNOON_PUTS_SHADOW_OFF=1``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

SHADOW_TARGET_PCT = 150.0
SHADOW_STOP_PCT = 40.0  # the desk's stop, unchanged
SHADOW_FLAT_CT = time(14, 45)
AFTER_NOON_CT = time(12, 0)
SHADOW_DAYS = 30
DEFAULT_POLL_SEC = 20.0
_KEEP_DAYS = 60
_LOCK = threading.Lock()
_last_poll: dict[str, datetime] = {}


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def shadow_path() -> Path:
    raw = str(os.environ.get("AFTERNOON_PUTS_SHADOW_PATH") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "outputs" / "afternoon_puts_shadow.json"


def shadow_off() -> bool:
    return str(os.environ.get("AFTERNOON_PUTS_SHADOW_OFF") or "").strip().lower() in {"1", "true", "yes", "on"}


def poll_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("AFTERNOON_PUTS_SHADOW_POLL_SEC") or DEFAULT_POLL_SEC))
    except (TypeError, ValueError):
        return DEFAULT_POLL_SEC


def _read(path: Path | None = None) -> dict[str, Any]:
    target = path or shadow_path()
    try:
        blob = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _write(blob: dict[str, Any], path: Path | None = None) -> None:
    target = path or shadow_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(blob, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def _start_day(blob: dict[str, Any], today: date) -> date:
    raw = str(os.environ.get("AFTERNOON_PUTS_SHADOW_START") or "").strip() or str(blob.get("started_day") or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return today


def window(blob: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Shadow window: first day, last day, and whether it still polls today."""
    today = now.astimezone(CT).date()
    start = _start_day(blob, today)
    end = start + timedelta(days=SHADOW_DAYS - 1)
    off = shadow_off()
    return {
        "started_day": start.isoformat(),
        "ends_day": end.isoformat(),
        "days_left": max(0, (end - today).days + 1) if today >= start else SHADOW_DAYS,
        "expired": today > end,
        "off": off,
        "active": (not off) and start <= today <= end,
    }


def _attr(order: Any, name: str) -> Any:
    if isinstance(order, dict):
        return order.get(name)
    return getattr(order, name, None)


def _meta(order: Any) -> dict[str, Any]:
    return dict(_attr(order, "meta") or {})


def _is_entry(order: Any) -> bool:
    meta = _meta(order)
    if str(_attr(order, "action") or "").upper() != "BUY":
        return False
    if str(_attr(order, "desk") or "") != "afternoon_puts" and str(meta.get("golden_kind") or "") != "afternoon_puts":
        return False
    if str(_attr(order, "status") or "") not in {"filled", "partial"}:
        return False
    fill = _f(_attr(order, "fill_price"))
    return fill is not None and fill > 0


def _entry_time(order: Any) -> datetime | None:
    meta = _meta(order)
    return _parse(meta.get("wb_filled_time_at")) or _parse(_attr(order, "created_at"))


def _qty(order: Any) -> int:
    meta = _meta(order)
    filled = _f(meta.get("filled_qty"))
    if filled is not None and filled > 0:
        return int(filled + 1e-9)
    return int(_f(_attr(order, "quantity")) or 1)


def real_exit(buy: Any, orders: list[Any]) -> dict[str, Any] | None:
    """Our filled sells on this lot: average fill, last fill time, reasons."""
    buy_id = str(_attr(buy, "id") or "")
    contract = str(_attr(buy, "contract") or "")
    created = str(_attr(buy, "created_at") or "")
    qty = 0.0
    notional = 0.0
    last_at = ""
    reasons: list[str] = []
    for order in orders:
        if str(_attr(order, "action") or "").upper() != "SELL":
            continue
        if str(_attr(order, "status") or "") not in {"filled", "partial"}:
            continue
        meta = _meta(order)
        names = str(meta.get("closes_order_id") or "") == buy_id
        same = bool(contract) and str(_attr(order, "contract") or "") == contract
        if same and created and str(_attr(order, "created_at") or "") < created:
            same = False
        if not (names or same):
            continue
        fill = _f(_attr(order, "fill_price"))
        n = _f(meta.get("filled_qty")) or (_f(_attr(order, "quantity")) or 1.0)
        if fill is None or n <= 0:
            continue
        qty += n
        notional += fill * n
        at = str(meta.get("wb_filled_time_at") or _attr(order, "updated_at") or _attr(order, "created_at") or "")
        last_at = max(last_at, at)
        reason = str(_attr(order, "reason") or "")[:80]
        if reason and reason not in reasons:
            reasons.append(reason)
    if qty <= 0:
        return None
    return {"fill": round(notional / qty, 4), "qty": qty, "at": last_at, "reason": " · ".join(reasons)}


def _new_row(order: Any) -> dict[str, Any]:
    fill = float(_f(_attr(order, "fill_price")) or 0)
    entered = _entry_time(order)
    entered_ct = entered.astimezone(CT) if entered else None
    meta = _meta(order)
    return {
        "id": str(_attr(order, "id") or ""),
        "day": entered_ct.date().isoformat() if entered_ct else str(_attr(order, "expiry") or "")[:10],
        "symbol": str(_attr(order, "symbol") or "").upper(),
        "contract": str(_attr(order, "contract") or ""),
        "expiry": str(_attr(order, "expiry") or "")[:10],
        "strike": _f(_attr(order, "strike")),
        "qty": _qty(order),
        "signal_at_ct": meta.get("signal_at_ct"),
        "entry_fill": fill,
        "entry_at": entered.isoformat() if entered else None,
        "entry_ct": entered_ct.strftime("%H:%M") if entered_ct else None,
        "after_noon": bool(entered_ct and entered_ct.time() >= AFTER_NOON_CT),
        "shadow_target": round(fill * (1.0 + SHADOW_TARGET_PCT / 100.0), 2),
        "shadow_stop": round(fill * (1.0 - SHADOW_STOP_PCT / 100.0), 2),
        "shadow_rule": "sell all at +150% (Limit), −40% stop, flat 2:45 PM CT",
        "max_bid": None,
        "max_bid_at": None,
        "last_bid": None,
        "last_quote_at": None,
        "quotes": 0,
        "touched_150": False,
        "shadow_done": False,
        "shadow_exit": None,
        "shadow_exit_at": None,
        "shadow_exit_why": None,
        "shadow_pnl_usd": None,
        "real_exit": None,
        "real_pnl_usd": None,
    }


def _close(row: dict[str, Any], price: float, at: datetime, why: str) -> None:
    row["shadow_done"] = True
    row["shadow_exit"] = round(float(price), 4)
    row["shadow_exit_at"] = at.isoformat()
    row["shadow_exit_why"] = why
    row["shadow_pnl_usd"] = round((float(price) - float(row["entry_fill"])) * 100.0 * int(row.get("qty") or 1), 2)


def _quote_bid(row: dict[str, Any], quote: Callable[[dict[str, Any]], Any]) -> float | None:
    try:
        got = quote(
            {
                "symbol": row["symbol"],
                "expiry": row["expiry"],
                "strike": row["strike"],
                "right": "P",
                "contract": row["contract"],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("afternoon shadow quote failed for %s: %s", row.get("contract"), type(exc).__name__)
        return None
    if not isinstance(got, dict):
        return None
    bid = _f(got.get("bid"))
    return bid if bid is not None and bid >= 0 else None


def _observe(row: dict[str, Any], bid: float, at: datetime) -> None:
    row["quotes"] = int(row.get("quotes") or 0) + 1
    row["last_bid"] = bid
    row["last_quote_at"] = at.isoformat()
    if row.get("max_bid") is None or bid > float(row["max_bid"]):
        row["max_bid"] = bid
        row["max_bid_at"] = at.isoformat()
    if bid + 1e-9 >= float(row["shadow_target"]):
        row["touched_150"] = True
    if row.get("shadow_done"):
        return
    if bid + 1e-9 >= float(row["shadow_target"]):
        # A resting Limit at the target fills at the target.
        _close(row, float(row["shadow_target"]), at, f"+150% target hit (bid ${bid:.2f}, Limit ${row['shadow_target']:.2f})")
    elif bid <= float(row["shadow_stop"]) + 1e-9 and bid > 0:
        _close(row, bid, at, f"−40% stop (bid ${bid:.2f})")


def tick(
    orders: list[Any],
    now: datetime | None = None,
    *,
    quote: Callable[[dict[str, Any]], Any] | None = None,
    path: Path | None = None,
    force_poll: bool = False,
) -> dict[str, Any]:
    """One shadow pass from the exit loop. Reads quotes, writes the log, sends nothing."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if shadow_off():
        return {"skipped": "AFTERNOON_PUTS_SHADOW_OFF"}
    if quote is None:
        from odte_scanner.options.contract_quote import fetch_contract_quote

        quote = fetch_contract_quote
    with _LOCK:
        blob = _read(path)
        win = window(blob, moment)
        if not win["active"]:
            return {"skipped": "shadow window over" if win["expired"] else "shadow window not started", **win}
        changed = False
        if not blob.get("started_day"):
            blob["started_day"] = win["started_day"]
            changed = True
        rows: dict[str, Any] = blob.setdefault("rows", {})
        now_ct = moment.astimezone(CT)
        today = now_ct.date().isoformat()
        start = win["started_day"]
        polled = 0
        for order in list(orders or []):
            if not _is_entry(order):
                continue
            row_id = str(_attr(order, "id") or "")
            row = rows.get(row_id)
            if row is None:
                fresh = _new_row(order)
                if fresh["day"] < start:
                    continue
                rows[row_id] = row = fresh
                changed = True
            real = real_exit(order, orders)
            if real is not None and real != row.get("real_exit"):
                row["real_exit"] = real
                row["real_pnl_usd"] = round((real["fill"] - float(row["entry_fill"])) * 100.0 * real["qty"], 2)
                changed = True
            if row.get("shadow_done") or row["day"] != today or row["expiry"] != today:
                if not row.get("shadow_done") and row["day"] < today:
                    # Missed the session close (loop down): close at the last quote seen.
                    last = _f(row.get("last_bid"))
                    if last is not None:
                        _close(row, last, _parse(row.get("last_quote_at")) or moment, "flat 2:45 PM CT (last quote seen)")
                    else:
                        row["shadow_done"] = True
                        row["shadow_exit_why"] = "no live quote before the close"
                    changed = True
                continue
            key = row["contract"]
            last_poll = _last_poll.get(key)
            due = force_poll or last_poll is None or (moment - last_poll).total_seconds() >= poll_sec()
            flat_due = now_ct.time() >= SHADOW_FLAT_CT
            if not due and not flat_due:
                continue
            bid = _quote_bid(row, quote)
            _last_poll[key] = moment
            polled += 1
            if bid is None:
                if flat_due and row.get("last_bid") is not None:
                    _close(row, float(row["last_bid"]), moment, "flat 2:45 PM CT (last quote seen)")
                    changed = True
                continue
            if flat_due:
                row["quotes"] = int(row.get("quotes") or 0) + 1
                row["last_bid"] = bid
                row["last_quote_at"] = moment.isoformat()
                _close(row, bid, moment, f"flat 2:45 PM CT (bid ${bid:.2f})")
            else:
                _observe(row, bid, moment)
            changed = True
        cutoff = (now_ct.date() - timedelta(days=_KEEP_DAYS)).isoformat()
        for row_id in [rid for rid, row in rows.items() if str(row.get("day") or "") < cutoff]:
            rows.pop(row_id, None)
            changed = True
        if changed:
            blob["updated_at"] = moment.isoformat()
            _write(blob, path)
        return {"ok": True, "polled": polled, "rows": len(rows), **win}


def summary(*, days: int = 30, now: datetime | None = None, path: Path | None = None) -> dict[str, Any]:
    """Shadow vs real for the endpoint and the tile. Reads the log only."""
    moment = now or datetime.now(timezone.utc)
    blob = _read(path)
    win = window(blob, moment)
    since = (moment.astimezone(CT).date() - timedelta(days=max(1, int(days)) - 1)).isoformat()
    rows = sorted(
        (row for row in (blob.get("rows") or {}).values() if str(row.get("day") or "") >= since),
        key=lambda row: (str(row.get("day") or ""), str(row.get("entry_at") or "")),
        reverse=True,
    )

    def bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
        done = [row for row in items if row.get("shadow_pnl_usd") is not None]
        real = [row for row in items if row.get("real_pnl_usd") is not None]
        return {
            "entries": len(items),
            "shadow_closed": len(done),
            "hit_150": sum(1 for row in items if str(row.get("shadow_exit_why") or "").startswith("+150%")),
            "touched_150": sum(1 for row in items if row.get("touched_150")),
            "shadow_pnl_usd": round(sum(float(row["shadow_pnl_usd"]) for row in done), 2),
            "real_pnl_usd": round(sum(float(row["real_pnl_usd"]) for row in real), 2),
            "real_closed": len(real),
        }

    return {
        "ok": True,
        "shadow_only": True,
        "orders": "none — logging only",
        "rule": "sell the whole position at +150% (Limit at fill × 2.5); −40% stop and 2:45 PM CT flat unchanged",
        "after_noon_tag": "entry filled at or after 12:00 PM CT",
        "kill_switch": "AFTERNOON_PUTS_SHADOW_OFF=1",
        **win,
        "days": days,
        "totals": bucket(rows),
        "after_noon": bucket([row for row in rows if row.get("after_noon")]),
        "before_noon": bucket([row for row in rows if not row.get("after_noon")]),
        "rows": rows,
        "updated_at": blob.get("updated_at"),
    }


def tile_block(now: datetime | None = None, path: Path | None = None) -> dict[str, Any]:
    """Short view for the Afternoon Puts tile (no rows beyond today's)."""
    try:
        full = summary(now=now, path=path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}
    today = (now or datetime.now(timezone.utc)).astimezone(CT).date().isoformat()
    return {
        "active": full["active"],
        "off": full["off"],
        "ends_day": full["ends_day"],
        "days_left": full["days_left"],
        "totals": full["totals"],
        "after_noon": full["after_noon"],
        "today": {
            row["contract"]: {
                k: row.get(k)
                for k in ("shadow_target", "shadow_done", "shadow_exit", "shadow_exit_why", "shadow_pnl_usd", "after_noon", "max_bid", "touched_150")
            }
            for row in full["rows"]
            if row.get("day") == today
        },
    }
