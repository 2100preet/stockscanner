"""Close expired paper lots at intrinsic value.

A filled buy that is still long when its contract expires never gets a sell
fill, so it used to drop out of P&L, the scorecard and daily P&L. This writes
one synthetic SELL per contract at intrinsic value (underlying close on the
expiry date minus strike for a call, strike minus close for a put, floored at
0). An in-the-money expiry is flagged: the sandbox may exercise it, so the
dollars are an estimate until Webull shows the result.

The row never goes to Webull. It has no broker id, status "filled", desk
"expiry_close", and ``meta.expiry_close``. If Webull history later shows a
real sell for the same lot, the import voids the synthetic row.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
EXPIRY_DESK = "expiry_close"
_FILLED = {"filled", "partial"}
_SKIP_SYMBOLS = {"SPX", "SPXW"}
CLOSE_AFTER = time(16, 20)

CloseFn = Callable[[str, date], "float | None"]


def enabled() -> bool:
    return str(os.environ.get("WEBULL_EXPIRY_CLOSEOUT", "1")).strip().lower() not in {"0", "false", "off", "no"}


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _exp_date(expiry: Any) -> date | None:
    try:
        return datetime.strptime(str(expiry or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_settled(expiry: Any, now: datetime | None = None) -> bool:
    """True after 4:20 PM ET on expiry day, or any later day."""
    exp = _exp_date(expiry)
    if exp is None:
        return False
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    et = moment.astimezone(ET)
    if exp < et.date():
        return True
    return exp == et.date() and et.time() >= CLOSE_AFTER


def intrinsic(right: str, strike: float, close: float) -> float:
    if str(right or "C").upper().startswith("P"):
        return round(max(0.0, float(strike) - float(close)), 2)
    return round(max(0.0, float(close) - float(strike)), 2)


def yahoo_close(symbol: str, day: date) -> float | None:
    """Regular-session close for ``day`` from Yahoo daily bars. None if not posted yet."""
    from odte_scanner.data.fetcher import fetch_history

    try:
        df = fetch_history(symbol, period="3mo", interval="1d", cache_max_age_hours=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("expiry close unavailable for %s: %s", symbol, exc)
        return None
    if df is None or getattr(df, "empty", True):
        return None
    for stamp, row in df.iterrows():
        try:
            when = stamp.date() if hasattr(stamp, "date") else None
        except Exception:  # noqa: BLE001
            when = None
        if when == day:
            return _num(row.get("Close"))
    return None


def _qty(order: Any) -> float:
    meta = getattr(order, "meta", None) or {}
    filled = _num(meta.get("filled_qty"))
    if filled is not None and filled > 0:
        return filled
    return float(getattr(order, "quantity", None) or 1)


def _is_lunch(order: Any) -> bool:
    meta = getattr(order, "meta", None) or {}
    desk = str(getattr(order, "desk", "") or "").lower()
    return desk in {"lunch", "lunch_credit"} or bool(meta.get("spread_id"))


def _live_sell(order: Any) -> bool:
    if getattr(order, "action", "") != "SELL" or getattr(order, "status", "") not in _FILLED:
        return False
    return not (getattr(order, "meta", None) or {}).get("expiry_voided")


def open_expired_lots(orders: list[Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Contracts still long in the ledger after expiry, one entry per contract."""
    lots: dict[str, dict[str, Any]] = {}
    for order in orders:
        contract = str(getattr(order, "contract", "") or "")
        if not contract or str(getattr(order, "symbol", "") or "").upper() in _SKIP_SYMBOLS:
            continue
        if _is_lunch(order):
            continue
        lot = lots.setdefault(contract, {"contract": contract, "bought": 0.0, "sold": 0.0, "cost": 0.0, "buys": []})
        if getattr(order, "action", "") == "BUY" and getattr(order, "status", "") in _FILLED:
            price = _num(getattr(order, "fill_price", None))
            if price is None or price <= 0:
                continue
            qty = _qty(order)
            lot["bought"] += qty
            lot["cost"] += price * qty
            lot["buys"].append(order)
        elif _live_sell(order):
            lot["sold"] += _qty(order)
    out: list[dict[str, Any]] = []
    for lot in lots.values():
        remaining = round(lot["bought"] - lot["sold"], 4)
        if remaining < 1 or not lot["buys"]:
            continue
        buys = sorted(lot["buys"], key=lambda o: str(getattr(o, "created_at", "") or ""))
        last = buys[-1]
        if not is_settled(getattr(last, "expiry", None), now):
            continue
        lot["remaining"] = int(remaining + 1e-9)
        lot["entry"] = round(lot["cost"] / lot["bought"], 4)
        lot["buy"] = last
        out.append(lot)
    return out


def _tier(buy: Any) -> str | None:
    meta = getattr(buy, "meta", None) or {}
    kind = str(meta.get("golden_kind") or getattr(buy, "desk", "") or "").lower()
    if kind in {"sniper", "diamond"}:
        return kind
    if kind in {"afternoon", "afternoon_puts"}:
        return "afternoon"
    if kind in {"sure_winner", "sure_winner_gold", "gold"}:
        return "gold"
    return None


def close_expired(
    broker: Any,
    now: datetime | None = None,
    *,
    close_fn: CloseFn | None = None,
    positions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write intrinsic-value closes for expired lots. Returns a summary."""
    from odte_scanner.trading.webull import WebullOrderIntent

    summary: dict[str, Any] = {"closed": 0, "itm": 0, "waiting_for_close": 0, "rows": []}
    lots = open_expired_lots(list(broker.orders), now)
    if not lots:
        return summary
    fetch = close_fn or yahoo_close
    closes: dict[tuple[str, date], float | None] = {}
    for lot in lots:
        buy = lot["buy"]
        symbol = str(buy.symbol or "").upper()
        exp = _exp_date(buy.expiry)
        strike = _num(buy.strike)
        if exp is None or strike is None:
            continue
        key = (symbol, exp)
        if key not in closes:
            closes[key] = fetch(symbol, exp)
        close = closes[key]
        if close is None or close <= 0:
            summary["waiting_for_close"] += 1
            continue
        value = intrinsic(buy.right, strike, close)
        itm = value > 0
        settled_at = datetime.combine(exp, time(16, 0), tzinfo=ET).astimezone(timezone.utc).isoformat()
        flags: list[str] = []
        if itm:
            flags.append("ITM at expiry — Webull may exercise; dollars are intrinsic until Webull shows the result")
        if (buy.meta or {}).get("position_gone"):
            flags.append("Webull showed no position before expiry — unverified")
        if positions and lot["contract"] in positions:
            flags.append("Webull still lists this position")
        state = "expired ITM" if itm else "expired worthless"
        reason = f"{state} — closed at intrinsic ${value:.2f} (underlying close ${close:.2f})"
        meta: dict[str, Any] = {
            "expiry_close": True,
            "itm": itm,
            "underlying_close": round(close, 4),
            "intrinsic": value,
            "closes_order_id": buy.id,
            "entry_price": lot["entry"],
            "entry_at": buy.created_at,
            "filled_qty": lot["remaining"],
            "fill_state": "filled",
            "source": "expiry_close",
            "flags": flags,
            "paper": getattr(broker, "paper", True),
            "sandbox": getattr(broker, "sandbox", True),
            "golden_kind": (buy.meta or {}).get("golden_kind"),
        }
        tier = _tier(buy)
        if tier:
            meta["exit_tier"] = tier
        meta["source_desk"] = {"gold": "golden_exit", "afternoon": "afternoon_puts"}.get(tier or "", tier or buy.desk)
        row = WebullOrderIntent(
            id=f"exp-{buy.id}-{exp.isoformat()}",
            desk=EXPIRY_DESK,
            action="SELL",
            symbol=symbol,
            right=buy.right,
            contract=lot["contract"],
            expiry=buy.expiry,
            strike=strike,
            quantity=lot["remaining"],
            limit_price=None,
            fill_price=value,
            reason=reason,
            status="filled",
            created_at=settled_at,
            updated_at=settled_at,
            meta=meta,
        )
        if any(o.id == row.id for o in broker.orders):
            continue
        broker.orders.append(row)
        summary["closed"] += 1
        summary["itm"] += int(itm)
        summary["rows"].append(
            {"id": row.id, "contract": row.contract, "qty": row.quantity, "intrinsic": value, "itm": itm, "flags": flags}
        )
        try:
            from odte_scanner.edge.round_trips import record_sell_fill

            record_sell_fill(row, broker.orders)
        except Exception as exc:  # noqa: BLE001
            logger.warning("expiry round trip was not recorded: %s", exc)
    if summary["closed"]:
        broker.orders.sort(key=lambda o: str(o.created_at or ""), reverse=True)
        broker.save()
    return summary


def void_for_real_sell(orders: list[Any], sell: Any) -> bool:
    """A real Webull sell showed up for a lot that was closed at expiry. Void the estimate."""
    voided = False
    for order in orders:
        meta = getattr(order, "meta", None) or {}
        if not meta.get("expiry_close") or meta.get("expiry_voided"):
            continue
        if getattr(order, "contract", None) != getattr(sell, "contract", None):
            continue
        order.status = "cancelled"
        meta["expiry_voided"] = True
        meta["voided_by"] = getattr(sell, "id", "")
        meta["fill_state"] = "cancelled"
        order.meta = meta
        voided = True
    return voided
