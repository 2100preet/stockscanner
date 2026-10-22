"""Reject option marks that cannot belong to the labeled contract.

A suggested-zone strike/expiry must not inherit the ask from a different OCC
symbol. Near-the-money premium that is far too cheap for its DTE is the same
class of bad print.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

_OCC = re.compile(r"([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})")

# Copied together or not at all. Splitting these is how a zone label picked up
# another contract's ask.
QUOTE_FIELDS = (
    "contract",
    "expiry",
    "dte",
    "strike",
    "ask",
    "bid",
    "option_last",
    "mark_source",
    "moneyness_pct",
    "open_interest",
    "volume",
    "target_ask",
    "debit_usd",
    "contracts_for_bankroll",
)


def parse_occ(contract: Any) -> dict[str, Any] | None:
    raw = str(contract or "").upper().replace("O:", "").replace(" ", "")
    match = _OCC.search(raw)
    if not match:
        return None
    _root, yymmdd, right, strike_raw = match.groups()
    return {
        "right": right,
        "expiry": f"{2000 + int(yymmdd[:2]):04d}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "strike": int(strike_raw) / 1000.0,
    }


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _dte(row: dict[str, Any]) -> int | None:
    raw = row.get("dte")
    if raw is not None and raw != "":
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    text = str(row.get("expiry") or "")[:10]
    try:
        expiry = date.fromisoformat(text)
    except ValueError:
        return None
    return (expiry - date.today()).days


def _spot(row: dict[str, Any]) -> float | None:
    for key in ("spot", "live_last", "live_spot", "last_price", "underlying_price"):
        number = _num(row.get(key))
        if number is not None and number > 0:
            return number
    return None


def identity_conflict(row: dict[str, Any]) -> str | None:
    """OCC symbol disagrees with the strike, expiry, or right on the row."""
    parts = parse_occ(row.get("contract") or row.get("occ"))
    if not parts:
        return None
    strike = _num(row.get("strike"))
    if strike is not None and abs(strike - float(parts["strike"])) > 0.02:
        return "contract symbol strike does not match the labeled strike"
    expiry = str(row.get("expiry") or "")[:10]
    if len(expiry) == 10 and expiry != parts["expiry"]:
        return "contract symbol expiry does not match the labeled expiry"
    right = str(row.get("right") or "").strip().upper()[:1]
    if right in {"C", "P"} and right != parts["right"]:
        return "contract symbol right does not match the labeled right"
    return None


def quote_reject_reason(row: dict[str, Any] | None) -> str | None:
    """Why this mark must not be alerted, logged, or routed. None means usable.

    Rows that do not carry enough fields to judge are left alone so desks that
    omit spot or strike are not all dropped.
    """
    if not isinstance(row, dict):
        return "missing quote"
    if row.get("suggested_zone") or str(row.get("mark_source") or "") == "zone":
        return "suggested zone is not a live quote"
    conflict = identity_conflict(row)
    if conflict:
        return conflict
    if row.get("quote_stale") is True:
        return "quote is marked stale"

    ask = _num(row.get("ask"))
    spot = _spot(row)
    strike = _num(row.get("strike"))
    if ask is None or ask <= 0 or spot is None or strike is None or strike <= 0:
        return None

    dte = _dte(row)
    gap = abs(strike - spot) / spot
    near_atm = gap <= 0.03
    right = str(row.get("right") or "C").strip().upper()[:1]
    if right == "P":
        intrinsic = max(0.0, strike - spot)
    else:
        intrinsic = max(0.0, spot - strike)
    if intrinsic >= 0.5 and ask < intrinsic * 0.5:
        return "ask is far below intrinsic value"
    if near_atm and dte is not None and dte >= 3 and ask < spot * 0.005:
        return "near-the-money ask is under 0.5% of spot"

    bid = row.get("bid")
    bid_n = _num(bid) if bid is not None and bid != "" else None
    if (
        bid is not None
        and bid != ""
        and bid_n is not None
        and bid_n <= 0
        and near_atm
        and dte is not None
        and dte >= 3
    ):
        return "bid is zero"
    if (
        bid_n is not None
        and bid_n > 0
        and ask > bid_n
        and (ask - bid_n) / ask > 0.50
        and near_atm
        and dte is not None
        and dte >= 3
    ):
        return "spread is wider than 50%"

    for key in ("quote_asof", "quote_ts", "quoted_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age > 45 * 60:
            return "quote timestamp is stale"
        break
    return None


def quote_bundle_ok(row: dict[str, Any] | None) -> bool:
    return quote_reject_reason(row) is None


def merge_prior_quote(ticket: dict[str, Any], prev: dict[str, Any] | None) -> None:
    """Adopt an earlier live quote only as a whole, consistent contract.

    A zone ticket has its own strike and expiry. Copying just the previous
    ask and OCC symbol onto that label is what priced the Oct 30 50 call with
    the Oct 23 51 call's premium.
    """
    if not isinstance(ticket, dict) or not isinstance(prev, dict):
        return
    zone_like = (
        ticket.get("suggested_zone")
        or str(ticket.get("mark_source") or "") in {"", "zone"}
        or ticket.get("ask") in (None, "", 0)
    )
    if zone_like and quote_bundle_ok(prev):
        for key in QUOTE_FIELDS:
            if prev.get(key) not in (None, ""):
                ticket[key] = prev.get(key)
    reason = quote_reject_reason(ticket)
    if reason:
        ticket["ask"] = None
        ticket["bid"] = None
        ticket["option_last"] = None
        ticket["mark_source"] = "rejected"
        ticket["quote_reject"] = reason
