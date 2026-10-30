"""One Telegram message when a Diamond, Sniper, or Gold Setup paper exit fills.

The customer-facing line never says Sure Winner. A target or stop that is
still working after about two minutes sends the same line once, marked
pending, and a later fill does not send it again. SPX is not a Webull order;
its line uses the modeled exit.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEN = ROOT / "outputs" / "exit_alert_seen.json"
PENDING_AFTER_SEC = 120

_TIER_LABEL = {
    "diamond": ("💎", "DIAMOND"),
    "sniper": ("🎯", "SNIPER"),
    "gold": ("⭐", "GOLD SETUP"),
}
_TIER_TARGET = {"diamond": 30, "sniper": 25, "gold": 30}
_TIER_STOP = {"diamond": 40, "sniper": 40, "gold": 50}
_FILLED = {"filled", "partial"}
_WORKING = {"submitted", "partial"}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_ct(value: Any, *, now: datetime | None = None) -> datetime | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return parsed.astimezone(CT)


def clock_ct(value: Any, *, now: datetime | None = None) -> str:
    """`9:13 CT` in the morning and `1:11 PM CT` after noon."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match:
        hour = int(match.group(1))
        minute = match.group(2)
    else:
        moment = _as_ct(value, now=now)
        if moment is None and now is not None:
            moment = now.astimezone(CT) if now.tzinfo else now.replace(tzinfo=CT)
        if moment is None:
            return "— CT"
        hour = moment.hour
        minute = f"{moment.minute:02d}"
    suffix = ""
    show = hour
    if hour == 0:
        show = 12
    elif hour == 12:
        suffix = " PM"
    elif hour > 12:
        show = hour - 12
        suffix = " PM"
    return f"{show}:{minute}{suffix} CT"


def _compact_expiry(raw: Any) -> str:
    text = str(raw or "")[:10]
    parts = text.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[1])}/{int(parts[2])}"
    return text


def _strike_text(strike: Any, right: str) -> str:
    side = "P" if str(right or "").upper().startswith("P") else "C"
    number = _num(strike)
    if number is None:
        return side
    base = str(int(number)) if float(number).is_integer() else f"{number:g}"
    return f"{base}{side}"


def _money(amount: float) -> str:
    sign = "+" if amount > 0 else ("−" if amount < 0 else "")
    absolute = abs(amount)
    if abs(absolute - round(absolute)) < 0.005:
        body = f"${absolute:,.0f}"
    else:
        body = f"${absolute:,.2f}"
    if amount == 0:
        return "$0"
    return f"{sign}{body}"


def _pct(amount: float) -> str:
    sign = "+" if amount > 0 else ("−" if amount < 0 else "")
    return f"{sign}{abs(amount):.1f}%"


def plain_exit_reason(detail: str, *, tier: str = "") -> str:
    """Target, stop, 10:30 CT flat, trail, or scale-out. Never a product nickname."""
    text = str(detail or "").strip()
    low = text.lower()
    if "scale out" in low or "scale-out" in low or low in {"scale", "scale-out"}:
        return "Scale-out"
    if "trail" in low or low == "be":
        return "Trail"
    if "10:30" in text or "10:30" in low:
        return "10:30 CT flat"
    if "2:30" in text or "14:30" in low:
        return "2:30 PM CT flat"
    if low in {"flat", "flatten"}:
        return "10:30 CT flat"
    stop_rule = re.search(r"[≤<=]\s*[−\-]\s*(\d+(?:\.\d+)?)\s*%", text)
    target_rule = re.search(r"≥\s*\+?\s*(\d+(?:\.\d+)?)\s*%", text)
    if "stop" in low or low == "stop":
        pct = float(stop_rule.group(1)) if stop_rule else float(_TIER_STOP.get(tier, 50))
        return f"Stop −{pct:.0f}% hit"
    if "take profit" in low or "target" in low or low == "target":
        pct = float(target_rule.group(1)) if target_rule else float(_TIER_TARGET.get(tier, 30))
        return f"Target +{pct:.0f}% hit"
    return "Exit"


def format_exit_alert(
    *,
    tier: str,
    symbol: str,
    strike: Any,
    right: str,
    expiry: Any,
    contracts: int,
    entry: float,
    entry_at: Any,
    exit_price: float,
    exit_at: Any,
    reason: str,
    tally: str,
    pending: bool = False,
) -> str:
    """One line plus the tier's tally. `Sure Winner` is not in this copy."""
    emoji, name = _TIER_LABEL.get(tier, ("⭐", "GOLD SETUP"))
    qty = max(1, int(contracts or 1))
    noun = "contract" if qty == 1 else "contracts"
    pnl_pct = ((exit_price - entry) / entry * 100.0) if entry else 0.0
    pnl_usd = (exit_price - entry) * 100.0 * qty
    contract = _strike_text(strike, right)
    when_in = clock_ct(entry_at)
    when_out = clock_ct(exit_at)
    why = plain_exit_reason(reason, tier=tier)
    line = (
        f"{emoji} {name} EXIT — {str(symbol or '').upper()} {contract} {_compact_expiry(expiry)}"
        f" · {qty} {noun}"
        f" · Entry ${entry:.2f} @ {when_in} → Exit ${exit_price:.2f} @ {when_out}"
        f" · {_pct(pnl_pct)} / {_money(pnl_usd)}"
        f" · {why}"
    )
    if pending:
        line += " · pending fill"
    if tally:
        line += f" · {tally}"
    return line


def tally_text(pnls: list[float]) -> str:
    wins = sum(1 for value in pnls if value > 0)
    losses = sum(1 for value in pnls if value < 0)
    net = sum(pnls)
    return f"Today {wins}W/{losses}L · {_money(net)}"


def _tier_of(order: Any, orders: list[Any]) -> str | None:
    meta = getattr(order, "meta", None) or {}
    stamped = str(meta.get("exit_tier") or "").lower()
    if stamped in _TIER_LABEL:
        return stamped
    closes = str(meta.get("closes_order_id") or "")
    contract = str(getattr(order, "contract", "") or "")
    for other in orders:
        if str(getattr(other, "action", "") or "").upper() != "BUY":
            continue
        if closes and str(getattr(other, "id", "") or "") == closes:
            return _tier_of_buy(other)
        if contract and str(getattr(other, "contract", "") or "") == contract:
            found = _tier_of_buy(other)
            if found:
                return found
    return None


def _tier_of_buy(order: Any) -> str | None:
    meta = getattr(order, "meta", None) or {}
    kind = str(meta.get("golden_kind") or "").lower()
    desk = str(getattr(order, "desk", "") or "").lower()
    if kind == "diamond" or desk == "diamond":
        return "diamond"
    if kind == "sniper" or desk == "sniper":
        return "sniper"
    if kind in {"sure_winner_gold", "sure_winner"} or desk == "sure_winner":
        return "gold"
    return None


def _is_target_or_stop(detail: str) -> bool:
    low = str(detail or "").lower()
    if any(token in low for token in ("scale", "trail", "flatten", "10:30", "2:30", "time-stop", "time stop")):
        return False
    return "stop" in low or "take profit" in low or "target" in low


def _event_from_order(order: Any, orders: list[Any], now: datetime) -> dict[str, Any] | None:
    if str(getattr(order, "action", "") or "").upper() != "SELL":
        return None
    desk = str(getattr(order, "desk", "") or "")
    if desk not in {"golden_exit", "diamond", "sniper", "sure_winner"}:
        return None
    tier = _tier_of(order, orders)
    if tier is None:
        return None
    status = str(getattr(order, "status", "") or "")
    meta = getattr(order, "meta", None) or {}
    entry = _num(meta.get("entry_price"))
    if entry is None or entry <= 0:
        return None
    filled = status in _FILLED and _num(getattr(order, "fill_price", None)) not in (None, 0)
    pending = False
    if filled:
        exit_px = float(getattr(order, "fill_price"))
        exit_at = getattr(order, "updated_at", None) or now
    elif status in _WORKING and _is_target_or_stop(str(getattr(order, "reason", "") or "")):
        clock = _parse_dt(meta.get("exit_clock") or getattr(order, "created_at", None))
        if clock is None:
            return None
        age = (now.astimezone(timezone.utc) - clock.astimezone(timezone.utc)).total_seconds()
        if age < PENDING_AFTER_SEC:
            return None
        exit_px = _num(getattr(order, "limit_price", None))
        if exit_px is None or exit_px <= 0:
            return None
        exit_at = now
        pending = True
    else:
        return None
    try:
        qty = int(float(meta.get("filled_qty") or getattr(order, "quantity", None) or 1))
    except (TypeError, ValueError):
        qty = 1
    qty = max(1, qty)
    return {
        "order_id": str(getattr(order, "id", "") or ""),
        "tier": tier,
        "symbol": getattr(order, "symbol", ""),
        "strike": getattr(order, "strike", None),
        "right": getattr(order, "right", "") or "C",
        "expiry": getattr(order, "expiry", ""),
        "contracts": qty,
        "entry": entry,
        "entry_at": meta.get("entry_at") or "",
        "exit": exit_px,
        "exit_at": exit_at,
        "reason": str(getattr(order, "reason", "") or ""),
        "pending": pending,
        "pnl": (exit_px - entry) * 100.0 * qty,
    }


def _event_from_spx(row: dict[str, Any], *, tier: str) -> dict[str, Any] | None:
    if str(row.get("symbol") or "").upper() != "SPX":
        return None
    name = str(row.get("tier") or tier or "").lower()
    if name not in _TIER_LABEL:
        return None
    entry = _num(row.get("ask"))
    if entry is None or entry <= 0:
        entry = _num(row.get("premium"))
    pct = _num(row.get("option_pct"))
    if entry is None or entry <= 0 or pct is None:
        return None
    exit_px = entry * (1.0 + pct / 100.0)
    if exit_px <= 0:
        return None
    qty = 1
    day = str(row.get("day") or "")
    entry_at = str(row.get("at_ct") or "")
    exit_at = str(row.get("exit_at_ct") or entry_at)
    return {
        "order_id": str(row.get("order_id") or f"spx-{name}-{day}"),
        "tier": name,
        "symbol": "SPX",
        "strike": row.get("strike"),
        "right": row.get("right") or "C",
        "expiry": row.get("expiry") or day,
        "contracts": qty,
        "entry": entry,
        "entry_at": entry_at,
        "exit": exit_px,
        "exit_at": exit_at,
        "reason": str(row.get("reason") or ""),
        "pending": False,
        "pnl": (exit_px - entry) * 100.0 * qty,
        "day": day,
    }


def _day_of(event: dict[str, Any], now: datetime) -> str:
    stamped = str(event.get("day") or "")
    if len(stamped) >= 10 and stamped[4:5] == "-":
        return stamped[:10]
    moment = _as_ct(event.get("exit_at"), now=now)
    if moment is None:
        moment = now.astimezone(CT) if now.tzinfo else now.replace(tzinfo=CT)
    return moment.date().isoformat()


def collect_exit_alerts(
    orders: list[Any] | None,
    *,
    now: datetime,
    seen: set[str],
    spx_exits: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Alerts still owed. A seen order id is skipped, including a pending one that later fills."""
    moment = now if now.tzinfo else now.replace(tzinfo=CT)
    events: list[dict[str, Any]] = []
    for order in orders or []:
        event = _event_from_order(order, list(orders or []), moment)
        if event and event.get("order_id"):
            events.append(event)
    for row in spx_exits or []:
        if not isinstance(row, dict):
            continue
        event = _event_from_spx(row, tier=str(row.get("tier") or ""))
        if event and event.get("order_id"):
            events.append(event)
    today = moment.astimezone(CT).date().isoformat()
    out: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for event in events:
        order_id = str(event["order_id"])
        if not order_id or order_id in seen or order_id in claimed:
            continue
        claimed.add(order_id)
        day = _day_of(event, moment)
        pnls = [
            float(other["pnl"])
            for other in events
            if other["tier"] == event["tier"] and _day_of(other, moment) == day and not other["pending"]
        ]
        if event["pending"] and day == today:
            pnls.append(float(event["pnl"]))
        tally = tally_text(pnls)
        message = format_exit_alert(
            tier=event["tier"],
            symbol=event["symbol"],
            strike=event["strike"],
            right=event["right"],
            expiry=event["expiry"],
            contracts=int(event["contracts"]),
            entry=float(event["entry"]),
            entry_at=event["entry_at"],
            exit_price=float(event["exit"]),
            exit_at=event["exit_at"],
            reason=event["reason"],
            tally=tally,
            pending=bool(event["pending"]),
        )
        out.append({"order_id": order_id, "tier": event["tier"], "message": message, "pending": event["pending"]})
    return out


def _load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(raw, dict):
        raw = raw.get("ids") or raw.get("keys") or []
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}


def _save_seen(path: Path, ids: set[str]) -> None:
    from odte_scanner.json_util import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"ids": sorted(ids)}, indent=2)
    atomic_write_text(path, payload)


def dispatch_exit_alerts(
    orders: list[Any] | None = None,
    *,
    now: datetime | None = None,
    seen_path: str | Path | None = None,
    diamond: dict[str, Any] | None = None,
    sniper: dict[str, Any] | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Send at most one Telegram line per order id. Returns the lines that went out."""
    moment = now or datetime.now(CT)
    path = Path(seen_path) if seen_path else DEFAULT_SEEN
    seen = _load_seen(path)
    spx: list[dict[str, Any]] = []
    for board, tier in ((diamond, "diamond"), (sniper, "sniper")):
        if not isinstance(board, dict):
            continue
        for row in board.get("spx_exits") or []:
            if isinstance(row, dict):
                item = dict(row)
                item.setdefault("tier", tier)
                spx.append(item)
    pending = collect_exit_alerts(orders, now=moment, seen=seen, spx_exits=spx)
    if not pending:
        return []
    sender = send
    if sender is None:
        from odte_scanner.alerts.telegram import configured, send_telegram_text

        if not configured().get("ok"):
            return []
        sender = send_telegram_text
    sent: list[dict[str, Any]] = []
    changed = False
    for alert in pending:
        if alert["order_id"] in seen:
            continue
        try:
            result = sender(alert["message"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("exit alert failed for %s: %s", alert["order_id"], exc)
            continue
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        seen.add(alert["order_id"])
        changed = True
        sent.append(alert)
    if changed:
        _save_seen(path, seen)
    return sent
