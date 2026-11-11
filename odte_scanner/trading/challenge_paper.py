"""Webull paper switch for the $1k → $100k Challenge sleeve.

Default is off. ``CHALLENGE_PAPER=1`` or ``live_trading.challenge_paper: true``
turns on sandbox orders for the sleeve the journal already booked. The flag
never reads ``WEBULL_LIVE`` and never selects the production host.

Orders stay on the second paper account (``WEBULL_ACCOUNT_ID_2``). Sizing
stays at 1 contract on a same-day expiry and at most 2 otherwise. Daily loss
limits and the kill switch still run in the auto-trader, because those checks
are on the entry path and are not desk-specific.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}


def challenge_paper_enabled(live: dict[str, Any] | None = None) -> bool:
    """True only when the owner opts in. Env wins over config. Default off."""
    raw = (os.environ.get("CHALLENGE_PAPER") or "").strip().lower()
    if raw in _OFF:
        return False
    if raw in _ON:
        return True
    if not isinstance(live, dict):
        return False
    return bool(live.get("challenge_paper", False))


def pace_months_for_deadline(
    deadline: str | None,
    *,
    configured: float = 1.0,
    now: datetime | None = None,
) -> float:
    """Months of pace math for the board.

    A missing deadline, or one that has already passed, uses ``configured``
    (``challenge_pace_months``, default one month). It does not pretend a
    single day is left and compress the sprint.
    """
    from odte_scanner.signals.beauty_monthly import days_to_deadline

    months = float(configured) if configured and float(configured) > 0 else 1.0
    days = days_to_deadline(deadline, now=now)
    if days is None or days <= 0:
        return months
    return max(0.25, days / 30.4375)


def order_row_from_open_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    """One journal open, shaped as a Webull ENTRY row.

    Target and stop percents are left off. The auto-trader then keeps
    ``exits.challenge`` (+100% / −50%, scale-out, 15:45 ET flatten) instead
    of copying the journal's tighter sniper bank onto the order.
    """
    if not isinstance(trade, dict):
        return None
    if str(trade.get("status") or "open") != "open":
        return None
    symbol = str(trade.get("symbol") or "").upper()
    contract = str(trade.get("contract") or "")
    ask = trade.get("entry_ask")
    if ask in (None, "", 0):
        ask = trade.get("ask")
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return None
    if not symbol or not contract or ask_f <= 0 or contract.endswith("_SYN"):
        return None
    right = str(trade.get("right") or "C").upper()
    if right not in {"C", "P"}:
        right = "C"
    contracts = trade.get("contracts")
    try:
        contracts_n = int(contracts) if contracts not in (None, "") else None
    except (TypeError, ValueError):
        contracts_n = None
    row: dict[str, Any] = {
        "symbol": symbol,
        "right": right,
        "action": "ENTRY",
        "ask": ask_f,
        "bid": trade.get("bid") if trade.get("bid") not in (None, "", 0) else trade.get("mark"),
        "contract": contract,
        "expiry": trade.get("expiry"),
        "strike": trade.get("strike"),
        "dte": trade.get("dte_at_entry") if trade.get("dte_at_entry") is not None else trade.get("dte"),
        "thesis": trade.get("thesis") or trade.get("entry_reason") or "challenge sleeve",
        "from_challenge_journal": True,
    }
    if contracts_n is not None and contracts_n >= 1:
        row["contracts_for_bankroll"] = contracts_n
    spot = trade.get("spot")
    if spot not in (None, "", 0):
        row["spot"] = spot
    return row


def paper_orders_from_trades(trades: list[Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in trades or []:
        raw = trade.to_dict() if hasattr(trade, "to_dict") else trade
        if not isinstance(raw, dict):
            continue
        row = order_row_from_open_trade(raw)
        if row is not None:
            rows.append(row)
    return rows


def apply_challenge_paper_board(
    challenge: dict[str, Any],
    trades: list[Any] | None,
    live: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the orders the paper desk may send.

    When the flag is off, ``paper_orders`` is empty so a display ENTRY card
    is not a Webull buy. When it is on, the list is the journal's open
    flips — the trades the sleeve actually took — not every radar card.
    """
    enabled = challenge_paper_enabled(live)
    challenge["challenge_paper"] = enabled
    challenge["paper_orders"] = paper_orders_from_trades(trades) if enabled else []
    return challenge
