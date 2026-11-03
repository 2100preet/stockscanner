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
    """``filled``, ``working limit``, or ``cancelled``."""
    status = str((order or {}).get("status") or "").lower()
    if status in _FILLED:
        return "filled"
    if status in _CANCELLED:
        return "cancelled"
    if status in _WORKING:
        return "working limit"
    return status or "working limit"


def tier_of(order: dict[str, Any] | None) -> str | None:
    """Sniper, Diamond, or Gold. Other desks are not this book."""
    if not isinstance(order, dict):
        return None
    meta = _meta(order)
    for value in (meta.get("exit_tier"), meta.get("golden_kind"), order.get("desk")):
        text = str(value or "").lower()
        if text in {"sniper", "diamond"}:
            return text
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
    return out


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


def annotate_card(card: dict[str, Any], orders: list[dict[str, Any]], tier: str) -> dict[str, Any]:
    """Stamp the traded contract onto a signal card. The signal strike remains."""
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
    label = status_label(entry)
    fill = _num(entry.get("fill_price"))
    limit = _num(entry.get("limit_price"))
    if label == "filled" and fill is not None and fill > 0:
        out["fill_price"] = fill
        out["display_price"] = fill
        out["display_price_label"] = "Fill"
    elif label == "working limit" and limit is not None and limit > 0:
        out["display_price"] = limit
        out["display_price_label"] = "Working limit"
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
        out["exit_status_label"] = status_label(exit_order)
        exit_fill = _num(exit_order.get("fill_price"))
        if exit_fill is not None and exit_fill > 0:
            out["exit_fill"] = exit_fill
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


def round_trips(orders: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One row per filled sell. Entry is the sell's stored price or the buy fill."""
    book = [order for order in (orders or []) if isinstance(order, dict)]
    buys: dict[str, dict[str, Any]] = {}
    for order in book:
        if str(order.get("action") or "").upper() != "BUY":
            continue
        if str(order.get("status") or "").lower() not in _FILLED:
            continue
        if str(order.get("symbol") or "").upper() == "SPX":
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
        exit_px = _num(order.get("fill_price"))
        contract = str(order.get("contract") or "")
        buy = buys.get(contract)
        tier = tier_of(order) or (tier_of(buy) if buy is not None else None)
        if tier not in {"sniper", "diamond", "gold"} or exit_px is None or exit_px <= 0:
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
