"""Afternoon Puts paper-lot bookkeeping for the exit loop.

Three Nov 2026 paper incidents drove this module. Every helper here only
answers for an Afternoon Puts lot (desk ``afternoon_puts`` or golden kind
``afternoon_puts``). Callers in ``auto_trader`` gate on :func:`is_afternoon_lot`
and on a paper broker, so Sniper LIVE and the other desks keep their behavior.

* Nov 6 QQQ 655P: the lot dropped out of Webull's positions snapshot while it
  was still held. The exit loop read that as flat, so the −40% stop was never
  watched. The 2:45 PM CT flatten only went out at 2:57 PM CT, once the lot
  showed up again. The ledger now decides whether we are still long: the
  filled buy minus our own filled sells. Webull's snapshot can lower that
  count, but its silence can't zero it.
* Nov 19 / Nov 20: closes went out for lots that our own filled sells had
  already closed (and Nov 17/18 contracts that had expired). Webull rejected
  them ("close intent mismatches position direction" / "invalid market,
  symbol, option_type"). A lot the ledger shows flat, or an expired contract,
  is no longer held, and a close-intent reject marks the lot gone.
"""

from __future__ import annotations

from typing import Any

AFTERNOON_DESK = "afternoon_puts"
# Webull says the account is not long this contract. A retry can't fill.
FLAT_REJECT_CODES = frozenset({"OPENAPI_POSITION_ORDER_INTENT_MISMATCH"})
FLAT_REJECT_TEXT = (
    "close intent mismatches position direction",
    "invalid market,symbol,option_type",
)
_CLOSED_SELL_STATUSES = frozenset({"filled", "partial"})


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def is_afternoon_lot(order: Any) -> bool:
    if order is None:
        return False
    if str(getattr(order, "action", "") or "").upper() != "BUY":
        return False
    meta = getattr(order, "meta", None) or {}
    return str(getattr(order, "desk", "") or "") == AFTERNOON_DESK or str(meta.get("golden_kind") or "") == AFTERNOON_DESK


def _qty(order: Any) -> float:
    meta = getattr(order, "meta", None) or {}
    filled = _f(meta.get("filled_qty"))
    if filled is not None and filled > 0:
        return filled
    return float(getattr(order, "quantity", None) or 1)


def sold_qty(buy: Any, orders: list[Any]) -> float:
    """Contracts our own filled sells took off this lot.

    A sell counts when it names this buy (``closes_order_id``) or trades the
    same contract after the buy was placed. Each sell is counted once.
    """
    contract = str(getattr(buy, "contract", "") or "")
    created = str(getattr(buy, "created_at", "") or "")
    total = 0.0
    for order in orders:
        if str(getattr(order, "action", "") or "").upper() != "SELL":
            continue
        if str(getattr(order, "status", "") or "") not in _CLOSED_SELL_STATUSES:
            continue
        meta = getattr(order, "meta", None) or {}
        names_buy = str(meta.get("closes_order_id") or "") == str(getattr(buy, "id", "") or "")
        same_contract = bool(contract) and str(getattr(order, "contract", "") or "") == contract
        if same_contract and created and str(getattr(order, "created_at", "") or "") < created:
            same_contract = False
        if not (names_buy or same_contract):
            continue
        if str(getattr(order, "status", "") or "") == "partial":
            # Only the part Webull reports filled has left the account.
            total += max(0.0, _f(meta.get("filled_qty")) or 0.0)
        else:
            total += _qty(order)
    return total


def ledger_open_qty(buy: Any, orders: list[Any]) -> float:
    """Contracts still long by the ledger: the filled buy less our filled sells."""
    return max(0.0, _qty(buy) - sold_qty(buy, orders))


def is_flat_reject(code: Any, text: Any) -> bool:
    if str(code or "").upper() in FLAT_REJECT_CODES:
        return True
    low = str(text or "").lower()
    return any(part in low for part in FLAT_REJECT_TEXT)


def quote_mark(order: Any) -> tuple[float | None, str]:
    """Live bid for the exact contract when Webull gives no position mark.

    Tradier first, then Polygon (``fetch_contract_quote``). The bid is what a
    sell would hit. With no bid, we use the midpoint of a two-sided quote.
    """
    try:
        from odte_scanner.options.contract_quote import fetch_contract_quote

        quote = fetch_contract_quote(
            {
                "symbol": getattr(order, "symbol", None),
                "expiry": getattr(order, "expiry", None),
                "strike": getattr(order, "strike", None),
                "right": getattr(order, "right", None),
                "contract": getattr(order, "contract", None),
            }
        )
    except Exception:  # noqa: BLE001
        return None, "quote_error"
    if not quote:
        return None, "no_quote"
    bid = _f(quote.get("bid"))
    if bid is not None and bid > 0:
        return bid, f"{quote.get('source') or 'quote'}_bid"
    ask = _f(quote.get("ask"))
    if ask is not None and ask > 0 and bid is not None and bid >= 0:
        return round((ask + bid) / 2.0, 4), f"{quote.get('source') or 'quote'}_mid"
    return None, "no_bid"
