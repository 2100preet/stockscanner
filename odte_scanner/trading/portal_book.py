"""Portal view of the Webull paper book.

The signal card can name a different strike than the contract that actually
filled. Tiles, the Edge tab, and the daily P&L use the sandbox order: strike,
expiry, right, fill, and status. Signal strike stays as a secondary label.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_FILLED = {"filled", "partial"}
_WORKING = {"submitted", "staged", "deferred", "dry_run"}
_CANCELLED = {"cancelled", "rejected", "skipped"}
_RANK = {"filled": 0, "partial": 1, "submitted": 2, "staged": 3, "deferred": 3, "dry_run": 4, "cancelled": 5, "rejected": 6}

_PUBLIC_FIELDS = (
    "id",
    "desk",
    "action",
    "symbol",
    "right",
    "strike",
    "expiry",
    "contract",
    "quantity",
    "limit_price",
    "fill_price",
    "status",
    "reason",
    "created_at",
    "updated_at",
)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _meta(order: dict[str, Any]) -> dict[str, Any]:
    meta = order.get("meta")
    return meta if isinstance(meta, dict) else {}


def status_label(order: dict[str, Any] | None) -> str:
    """``filled``, ``working limit``, ``rejected``, or ``cancelled``."""
    status = str((order or {}).get("status") or "").lower()
    if status in _FILLED:
        return "filled"
    if status == "rejected":
        return "rejected"
    if status in _CANCELLED:
        return "cancelled"
    if status in _WORKING:
        return "working limit"
    return status or "working limit"


def public_desk_name(
    tier: str | None,
    order: dict[str, Any] | None = None,
    buy: dict[str, Any] | None = None,
) -> str:
    """Customer-facing desk. The internal sure-winner name is never returned."""
    if tier == "diamond":
        return "Diamond"
    if tier == "sniper":
        return "Sniper"
    if tier == "afternoon":
        return "Afternoon Puts"
    if tier == "lunch":
        return "Lunch-Break Credit"
    if tier == "gold":
        return "Gold Setup"
    desk = ""
    for src in (buy, order):
        if isinstance(src, dict) and src.get("desk"):
            desk = str(src.get("desk") or "")
            break
    from odte_scanner.edge.gate import source_for_webull

    name = source_for_webull(desk)
    low = name.lower().replace("-", " ").replace("_", " ")
    if "sure winner" in low or low in {"sure winner", "gold"}:
        return "Gold Setup"
    if low in {"afternoon", "afternoon puts"}:
        return "Afternoon Puts"
    if low in {"lunch", "lunch credit", "lunchcredit", "lunch break credit", "lunch-break credit"}:
        return "Lunch-Break Credit"
    if name in {"Diamond", "Sniper", "Gold Setup", "Afternoon Puts", "Lunch-Break Credit"}:
        return name
    if not name or name == "unknown":
        return "Other"
    return name


def tier_of(order: dict[str, Any] | None) -> str | None:
    """Sniper, Diamond, Afternoon Puts, or Gold. Other desks are not this book."""
    if not isinstance(order, dict):
        return None
    meta = _meta(order)
    for value in (meta.get("exit_tier"), meta.get("golden_kind"), order.get("desk")):
        text = str(value or "").lower()
        if text in {"sniper", "diamond"}:
            return text
        if text in {"afternoon", "afternoon_puts", "afternoonputs"}:
            return "afternoon"
        if text in {"lunch", "lunch_credit", "lunchcredit"}:
            return "lunch"
        if text in {"sure_winner", "sure_winner_gold", "gold"}:
            return "gold"
    return None


def public_order(order: dict[str, Any]) -> dict[str, Any]:
    """Allow-listed fields. Account ids and credentials stay off the payload."""
    out = {key: order.get(key) for key in _PUBLIC_FIELDS}
    out["golden_kind"] = _meta(order).get("golden_kind")
    out["exit_tier"] = _meta(order).get("exit_tier")
    out["entry_price"] = _meta(order).get("entry_price")
    out["status_label"] = status_label(order)
    out["tier"] = tier_of(order)
    meta = _meta(order)
    if str(order.get("status") or "").lower() in {"rejected", "cancelled", "deferred"} or meta.get("reject_code"):
        out["error"] = reject_text(order)
        out["reject_code"] = meta.get("reject_code")
        out["next_step"] = meta.get("next_step")
    return out


def reject_text(order: dict[str, Any] | None) -> str | None:
    """Plain Webull reason for a rejected/cancelled order. No request ids or account ids."""
    if not isinstance(order, dict):
        return None
    meta = _meta(order)
    reason = meta.get("reject_reason") or meta.get("cancel_reason")
    if not reason:
        raw = str(order.get("error") or "")
        if raw:
            from odte_scanner.trading.webull import parse_webull_error

            code, msg = parse_webull_error(raw)
            reason = msg or code
    return str(reason)[:240] if reason else None


def _best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: _RANK.get(str(row.get("status") or "").lower(), 9))[0]


def _match(orders: list[dict[str, Any]], *, symbol: str, tier: str, action: str) -> list[dict[str, Any]]:
    sym = str(symbol or "").upper()
    want = str(action or "").upper()
    found: list[dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or "").upper() != sym or str(order.get("symbol") or "").upper() == "SPX":
            continue
        if str(order.get("action") or "").upper() != want:
            continue
        if tier_of(order) != tier:
            continue
        found.append(order)
    return found


def _lunch_order(order: dict[str, Any]) -> bool:
    meta = _meta(order)
    if meta.get("leg") or meta.get("exclude_pnl"):
        return False
    text = f"{order.get('desk') or ''} {meta.get('golden_kind') or ''} {meta.get('exit_tier') or ''}".lower()
    return "lunch" in text


def annotate_card(card: dict[str, Any], orders: list[dict[str, Any]], tier: str) -> dict[str, Any]:
    """Stamp the traded contract onto a signal card. The signal strike remains."""
    if tier == "lunch":
        return _annotate_lunch(card, orders)
    out = dict(card)
    symbol = str(card.get("symbol") or "").upper()
    if symbol == "SPX":
        return out
    buys = _match(orders, symbol=symbol, tier=tier, action="BUY")
    entry = _best(buys)
    if entry is None:
        return out
    traded_strike = entry.get("strike")
    signal_strike = card.get("signal_strike", card.get("strike"))
    out["signal_strike"] = signal_strike
    out["signal_ask"] = card.get("signal_ask", card.get("ask"))
    out["traded_strike"] = traded_strike
    out["traded_expiry"] = entry.get("expiry")
    out["traded_right"] = entry.get("right")
    out["traded_contract"] = entry.get("contract")
    out["order_status"] = entry.get("status")
    out["order_status_label"] = status_label(entry)
    traded = _num(traded_strike)
    signal = _num(signal_strike)
    out["strike_differs"] = bool(traded is not None and signal is not None and abs(traded - signal) > 0.001)
    if out["strike_differs"]:
        # Quotes on the signal card belong to the other strike. The tile
        # shows a live quote only when one was fetched for the traded contract.
        out["signal_ask"] = card.get("signal_ask", card.get("ask"))
        out["bid"] = None
        out["ask"] = None
    live_bid = _num(card.get("live_bid"))
    live_ask = _num(card.get("live_ask"))
    if live_bid is not None and live_bid > 0:
        out["bid"] = live_bid
        out["live_bid"] = live_bid
    if live_ask is not None and live_ask > 0:
        out["ask"] = live_ask
        out["live_ask"] = live_ask
    label = status_label(entry)
    fill = _num(entry.get("fill_price"))
    limit = _num(entry.get("limit_price"))
    if label == "filled" and fill is not None and fill > 0:
        out["fill_price"] = fill
        out["display_price"] = fill
        out["display_price_label"] = "Filled @"
    elif label == "working limit" and limit is not None and limit > 0:
        out["display_price"] = limit
        out["display_price_label"] = "Limit (working)"
    elif limit is not None and limit > 0:
        out["display_price"] = limit
        out["display_price_label"] = "Limit"
    contract = str(entry.get("contract") or "")
    sells = [
        order
        for order in orders
        if isinstance(order, dict)
        and str(order.get("action") or "").upper() == "SELL"
        and str(order.get("symbol") or "").upper() == symbol
        and (not contract or str(order.get("contract") or "") == contract)
        and (tier_of(order) in {None, tier})
    ]
    exit_order = _best([row for row in sells if str(row.get("status") or "").lower() in _FILLED]) or _best(sells)
    if exit_order is not None:
        exit_label = status_label(exit_order)
        out["exit_status_label"] = exit_label
        if exit_label in {"rejected", "cancelled"}:
            out["exit_error"] = reject_text(exit_order)
        exit_fill = _num(exit_order.get("fill_price"))
        exit_limit = _num(exit_order.get("limit_price"))
        if exit_label == "filled" and exit_fill is not None and exit_fill > 0:
            out["exit_fill"] = exit_fill
            out["exit_display"] = exit_fill
            out["exit_price_label"] = "Filled @"
        elif exit_limit is not None and exit_limit > 0:
            out["exit_display"] = exit_limit
            out["exit_price_label"] = "Limit (working)" if exit_label == "working limit" else "Limit"
    return out


def _annotate_lunch(card: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    """A credit is a sell to open and a buy to close. The tile keeps both legs."""
    out = dict(card)
    if str(card.get("symbol") or "").upper() == "SPX":
        return out
    symbol = str(card.get("symbol") or "").upper()
    opens = [
        order
        for order in orders
        if _lunch_order(order)
        and str(order.get("symbol") or "").upper() == symbol
        and str(order.get("action") or "").upper() == "SELL"
        and str(_meta(order).get("role") or "open") == "open"
    ]
    entry = _best(opens)
    if entry is None:
        return out
    out["order_status"] = entry.get("status")
    out["order_status_label"] = status_label(entry)
    fill = _num(entry.get("fill_price"))
    limit = _num(entry.get("limit_price"))
    label = status_label(entry)
    if label == "filled" and fill is not None and fill > 0:
        out["fill_price"] = fill
        out["credit"] = fill
        out["display_price"] = fill
        out["display_price_label"] = "Filled @"
    elif label == "working limit" and limit is not None and limit > 0:
        out["limit_price"] = limit
        out["display_price"] = limit
        out["display_price_label"] = "Limit (working)"
    spread_id = str(_meta(entry).get("spread_id") or entry.get("contract") or "")
    closes = [
        order
        for order in orders
        if _lunch_order(order)
        and str(_meta(order).get("role") or "") == "close"
        and str(_meta(order).get("spread_id") or order.get("contract") or "") == spread_id
    ]
    exit_order = _best([row for row in closes if str(row.get("status") or "").lower() in _FILLED]) or _best(closes)
    if exit_order is not None:
        exit_label = status_label(exit_order)
        out["exit_status_label"] = exit_label
        exit_fill = _num(exit_order.get("fill_price"))
        exit_limit = _num(exit_order.get("limit_price"))
        if exit_label == "filled" and exit_fill is not None and exit_fill > 0:
            out["exit_fill"] = exit_fill
            out["exit_debit"] = exit_fill
            out["exit_display"] = exit_fill
            out["exit_price_label"] = "Filled @"
        elif exit_limit is not None and exit_limit > 0:
            out["exit_display"] = exit_limit
            out["exit_price_label"] = "Limit (working)" if exit_label == "working limit" else "Limit"
    return out


def annotate_board(board: dict[str, Any] | None, orders: list[dict[str, Any]], tier: str) -> dict[str, Any] | None:
    if not isinstance(board, dict):
        return board
    out = dict(board)
    cards: list[Any] = []
    for card in board.get("signals") or []:
        if isinstance(card, dict):
            cards.append(annotate_card(card, orders, tier))
        else:
            cards.append(card)
    out["signals"] = cards
    return out


def annotate_sure(board: dict[str, Any] | None, orders: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(board, dict):
        return board
    out = dict(board)
    for key in ("winners", "buy_now"):
        rows: list[Any] = []
        for card in board.get(key) or []:
            if isinstance(card, dict):
                rows.append(annotate_card(card, orders, "gold"))
            else:
                rows.append(card)
        out[key] = rows
    return out


def _qty(order: dict[str, Any]) -> int:
    try:
        return max(1, int(float(order.get("quantity") or 1)))
    except (TypeError, ValueError):
        return 1


def round_trips(
    orders: list[dict[str, Any]] | None,
    *,
    include_other_desks: bool = False,
) -> list[dict[str, Any]]:
    """One row per filled sell. Entry is the sell's stored price or the buy fill.

    The default book is Diamond, Sniper, Afternoon Puts, Lunch-Break Credit,
    and Gold Setup — the same rows the Edge tab and the exit alerts price.
    ``include_other_desks`` keeps every other non-SPX Webull fill with the
    same entry rule. SPX is never a row. A lunch credit is a sell to open
    and a buy to close, so its dollars are credit minus debit.
    """
    book = [order for order in (orders or []) if isinstance(order, dict)]
    lunch = _lunch_trips(book)
    buys: dict[str, dict[str, Any]] = {}
    for order in book:
        if str(order.get("action") or "").upper() != "BUY":
            continue
        if str(order.get("status") or "").lower() not in _FILLED:
            continue
        if str(order.get("symbol") or "").upper() == "SPX":
            continue
        if _lunch_order(order):
            continue
        price = _num(order.get("fill_price"))
        contract = str(order.get("contract") or "")
        if price is None or price <= 0 or not contract or contract in buys:
            continue
        buys[contract] = order
    trips: list[dict[str, Any]] = []
    for order in book:
        if str(order.get("action") or "").upper() != "SELL":
            continue
        if str(order.get("status") or "").lower() not in _FILLED:
            continue
        if str(order.get("symbol") or "").upper() == "SPX":
            continue
        if _lunch_order(order):
            continue
        exit_px = _num(order.get("fill_price"))
        contract = str(order.get("contract") or "")
        buy = buys.get(contract)
        tier = tier_of(order) or (tier_of(buy) if buy is not None else None)
        routed = tier in {"sniper", "diamond", "gold", "afternoon"}
        if exit_px is None or exit_px <= 0:
            continue
        if not routed and not include_other_desks:
            continue
        entry = _num(_meta(order).get("entry_price"))
        if (entry is None or entry <= 0) and buy is not None:
            entry = _num(buy.get("fill_price"))
        if entry is None or entry <= 0:
            continue
        qty = _qty(order)
        pnl = (exit_px - entry) * 100.0 * qty
        signal_strike = None
        trips.append(
            {
                "tier": tier,
                "desk": public_desk_name(tier, order, buy),
                "symbol": str(order.get("symbol") or "").upper(),
                "contract": contract,
                "strike": order.get("strike"),
                "right": order.get("right"),
                "expiry": order.get("expiry"),
                "signal_strike": signal_strike,
                "status": status_label(order),
                "entry": entry,
                "exit": exit_px,
                "contracts": qty,
                "pnl": round(pnl, 2),
                "reason": str(order.get("reason") or ""),
                "order_id": str(order.get("id") or ""),
                "raw_desk": str((buy or {}).get("desk") or order.get("desk") or ""),
                "exited_at": str(order.get("updated_at") or order.get("created_at") or ""),
                "entered_at": str(
                    (buy or {}).get("updated_at")
                    or (buy or {}).get("created_at")
                    or _meta(order).get("entry_at")
                    or ""
                ),
            }
        )
    trips.extend(lunch)
    return trips


def _lunch_trips(book: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per filled lunch close. P&L is the credit minus the debit."""
    opens: dict[str, dict[str, Any]] = {}
    for order in book:
        if not _lunch_order(order):
            continue
        if str(order.get("action") or "").upper() != "SELL":
            continue
        if str(order.get("status") or "").lower() not in _FILLED:
            continue
        if str(_meta(order).get("role") or "open") != "open":
            continue
        key = str(_meta(order).get("spread_id") or order.get("contract") or "")
        if key and key not in opens:
            opens[key] = order
    trips: list[dict[str, Any]] = []
    for order in book:
        if not _lunch_order(order):
            continue
        if str(order.get("action") or "").upper() != "BUY":
            continue
        if str(order.get("status") or "").lower() not in _FILLED:
            continue
        if str(_meta(order).get("role") or "") != "close":
            continue
        if str(order.get("symbol") or "").upper() == "SPX":
            continue
        debit = _num(order.get("fill_price"))
        if debit is None or debit <= 0:
            continue
        key = str(_meta(order).get("spread_id") or order.get("contract") or "")
        opened = opens.get(key)
        credit = _num(_meta(order).get("entry_price"))
        if (credit is None or credit <= 0) and opened is not None:
            credit = _num(opened.get("fill_price"))
        if credit is None or credit <= 0:
            continue
        qty = _qty(order)
        pnl = (credit - debit) * 100.0 * qty
        trips.append(
            {
                "tier": "lunch",
                "desk": "Lunch-Break Credit",
                "symbol": str(order.get("symbol") or "").upper(),
                "contract": str(order.get("contract") or key),
                "strike": _meta(order).get("short_strike") or order.get("strike"),
                "right": "P",
                "expiry": order.get("expiry"),
                "signal_strike": _meta(order).get("long_strike"),
                "status": status_label(order),
                "entry": credit,
                "exit": debit,
                "contracts": qty,
                "pnl": round(pnl, 2),
                "reason": str(order.get("reason") or ""),
                "order_id": str(order.get("id") or ""),
                "raw_desk": "lunch_credit",
                "exited_at": str(order.get("updated_at") or order.get("created_at") or ""),
                "entered_at": str(
                    (opened or {}).get("updated_at")
                    or (opened or {}).get("created_at")
                    or ""
                ),
            }
        )
    return trips


def _unpriced_exits(orders: list[dict[str, Any]]) -> int:
    """Filled Sniper/Diamond/Gold sells that have no entry, so no alert dollars."""
    priced = {
        (
            str(trip.get("contract") or ""),
            round(float(trip["exit"]), 4),
            int(trip["contracts"]),
        )
        for trip in round_trips(orders)
    }
    missing = 0
    for order in orders:
        if str(order.get("action") or "").upper() != "SELL":
            continue
        if str(order.get("status") or "").lower() not in _FILLED:
            continue
        if str(order.get("symbol") or "").upper() == "SPX":
            continue
        contract = str(order.get("contract") or "")
        buy = next(
            (
                other
                for other in orders
                if str(other.get("action") or "").upper() == "BUY" and str(other.get("contract") or "") == contract
            ),
            None,
        )
        tier = tier_of(order) or (tier_of(buy) if isinstance(buy, dict) else None)
        if tier not in {"sniper", "diamond", "gold"}:
            continue
        exit_px = _num(order.get("fill_price"))
        key = (contract, round(exit_px or 0.0, 4), _qty(order))
        if exit_px is None or exit_px <= 0 or key not in priced:
            missing += 1
    return missing


def reconcile_pnl(orders: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Daily paper P&L from sandbox fills, and the same dollars the exit alerts use.

    Both sides use fill price minus entry fill, times 100, times contracts.
    ``match`` is false when a filled sell cannot be priced, because that fill
    would be missing from the exit Telegram too.
    """
    book = [order for order in (orders or []) if isinstance(order, dict)]
    trips = round_trips(book)
    fill_pnl = round(sum(float(trip["pnl"]) for trip in trips), 2)
    unpriced = _unpriced_exits(book)
    return {
        "fill_pnl": fill_pnl,
        "alert_pnl": fill_pnl,
        "match": unpriced == 0,
        "unpriced": unpriced,
        "n": len(trips),
        "trips": trips,
        "source": "webull_sandbox_fills",
        "asof": datetime.now(timezone.utc).isoformat(),
    }
