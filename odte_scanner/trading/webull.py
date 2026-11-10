"""Webull broker bridge — deep links + optional OpenAPI order placement.

Safety defaults:
  • dry_run=True unless live_trading.enabled and credentials are present
  • paper mode targets the official sandbox host (Webull "Paper Trading" OpenAPI)
  • Never stores passwords in the repo — use env WEBULL_APP_KEY / WEBULL_APP_SECRET
  • Golden-only routing is enforced by AutoTrader (Sure Winner gold + Golden flow)

Official SDK: pip install webull-openapi-python-sdk
Docs: https://developer.webull.com/apis/docs/sdk.md
Paper / sandbox: https://developer.webull.com/apis/docs/authentication/IndividualApplicationAPI.md
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from odte_scanner.trading.ticks import is_tick_reject, normalize_option_limit, rereound_after_reject

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "outputs" / "webull_orders.json"

# Official Trading API hosts. Sandbox is the paper-trading environment
# ("Using OpenAPI service in Paper Trading" → Sandbox Trading). It is isolated
# from the production brokerage account. Same option order API on both hosts.
SANDBOX_TRADE_HOST = "api.sandbox.webull.com"
PRODUCTION_TRADE_HOST = "api.webull.com"

# Sandbox Individual Cash account number. The order API wants Webull's internal
# account_id from get_account_list, not this number.
CASH_ACCOUNT_NUMBER = "DEA32RD3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_account_id(payload: Any, account_number: str) -> str | None:
    """Return the internal account_id whose account number matches.

    Walks a get_account_list payload. A match that is only the account number
    itself is ignored — that value is not the id the order API accepts.
    """
    target = str(account_number or "").strip().upper()
    if not target:
        return None
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            number = str(
                node.get("account_number")
                or node.get("accountNumber")
                or node.get("account_no")
                or ""
            ).strip().upper()
            aid = str(node.get("account_id") or node.get("accountId") or "").strip()
            if number == target and aid and aid.upper() != target:
                found.append(aid)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found[0] if found else None


@dataclass
class WebullOrderIntent:
    """Normalized option order the desk wants to send to Webull."""

    id: str
    desk: str  # lottery | odte | weekly | swing | challenge
    action: str  # BUY | SELL
    symbol: str
    right: str  # C | P
    contract: str | None
    expiry: str | None
    strike: float | None
    quantity: int
    limit_price: float | None
    fill_price: float | None = None
    order_type: str = "LIMIT"  # LIMIT | MARKET
    tif: str = "DAY"
    reason: str = ""
    hist_win_pct: float | None = None
    hist_samples: int | None = None
    status: str = "staged"  # staged | dry_run | submitted | deferred | filled | partial | cancelled | rejected | skipped
    broker_order_id: str | None = None
    deep_link: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def webull_option_deep_link(
    symbol: str,
    *,
    expiry: str | None = None,
    strike: float | None = None,
    right: str = "C",
) -> str:
    """Best-effort Webull app/web link for the underlying (strike preselect when possible).

    Webull deep-link schemes vary by platform build; the trade page for the
    underlying is the reliable hand-off. Users confirm the option leg in-app.
    """
    sym = quote(str(symbol or "").upper())
    base = f"https://www.webull.com/quote/{sym}"
    # Some clients honor query hints — harmless if ignored
    bits = []
    if expiry:
        bits.append(f"expiry={quote(str(expiry))}")
    if strike is not None:
        bits.append(f"strike={strike:g}")
    if right:
        bits.append(f"right={quote(str(right).upper())}")
    return base + (("?" + "&".join(bits)) if bits else "")


def webull_app_scheme(symbol: str) -> str:
    """Mobile app scheme fallback."""
    return f"webull://quote/{quote(str(symbol or '').upper())}"


def webull_trade_host(*, sandbox: bool) -> str:
    """HTTP host for Trading API order placement."""
    return SANDBOX_TRADE_HOST if sandbox else PRODUCTION_TRADE_HOST


_OCC_RE = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def parse_occ_contract(contract: str | None) -> dict[str, Any] | None:
    """Split an OCC option symbol into symbol, expiry, right, and strike.

    ``IREN261016P00030000`` is the IREN 2026-10-16 30 put. A leading ``O:``
    is ignored. Returns None when the string is not an OCC symbol.
    """
    raw = str(contract or "").upper().replace("O:", "").replace(" ", "").strip()
    match = _OCC_RE.search(raw)
    if not match:
        return None
    root, yymmdd, right, strike_raw = match.groups()
    expiry = f"{2000 + int(yymmdd[:2]):04d}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    strike = int(strike_raw) / 1000.0
    return {
        "symbol": root,
        "expiry": expiry,
        "right": right,
        "strike": strike,
        "contract": f"{root}{yymmdd}{right}{strike_raw}",
    }


def format_occ_contract(symbol: str, expiry: str, right: str, strike: float) -> str | None:
    """Build an OCC symbol from underlying, expiry, put/call, and strike.

    Webull positions identify the underlying on ``symbol`` and the strike on
    ``option_exercise_price``. The ledger matches holdings by OCC symbol.
    """
    raw_exp = str(expiry or "")[:10]
    if len(raw_exp) != 10 or raw_exp[4] != "-" or raw_exp[7] != "-":
        return None
    root = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
    if not root or len(root) > 6:
        return None
    try:
        strike_i = int(round(float(strike) * 1000))
    except (TypeError, ValueError):
        return None
    if strike_i <= 0:
        return None
    side = "P" if str(right or "").upper() in {"P", "PUT"} else "C"
    yymmdd = raw_exp[2:4] + raw_exp[5:7] + raw_exp[8:10]
    return f"{root}{yymmdd}{side}{strike_i:08d}"


_LEG_LIST_KEYS = ("legs", "leg_list", "option_legs")
_COST_KEYS = (
    "cost_price",
    "costPrice",
    "avg_price",
    "avgPrice",
    "average_price",
    "averagePrice",
    "avg_cost",
    "average_cost",
    "unit_cost",
    "cost",
)
_FILL_PRICE_KEYS = (
    "avg_filled_price",
    "avgFilledPrice",
    "filled_price",
    "filledPrice",
    "avg_fill_price",
    "avg_price",
    "average_price",
    "avgPrice",
    "averagePrice",
)


def parse_option_positions(payload: Any) -> list[dict[str, Any]]:
    """Pull long option positions out of a Webull positions payload.

    Each row has contract, symbol, right, expiry, strike, quantity, and cost
    when the payload has one. The list endpoint returns a JSON array. Put/call
    and expiry live on ``legs``, and the strike is ``option_exercise_price``
    (not ``strike_price``). Quantity and cost often sit on the parent. Short
    or zero quantities are dropped. An OCC symbol on the row wins over a
    conflicting right or expiry field.
    """
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        legs = _position_legs(node)
        if legs:
            parsed_leg = False
            for leg in legs:
                ident = _position_identity(_leg_with_parent(node, leg))
                if ident is not None:
                    found.append(ident)
                    parsed_leg = True
            if not parsed_leg:
                ident = _position_identity(node)
                if ident is not None:
                    found.append(ident)
            for key, value in node.items():
                if key in _LEG_LIST_KEYS:
                    continue
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
            return
        ident = _position_identity(node)
        if ident is not None:
            found.append(ident)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, depth + 1)

    walk(payload)
    by_contract: dict[str, dict[str, Any]] = {}
    for row in found:
        key = str(row.get("contract") or "")
        if not key:
            key = f"{row.get('symbol')}|{row.get('right')}|{row.get('expiry')}|{row.get('strike')}"
        prev = by_contract.get(key)
        if prev is None or float(row["quantity"]) > float(prev["quantity"]):
            by_contract[key] = row
        elif float(row["quantity"]) == float(prev["quantity"]) and row.get("cost") and not prev.get("cost"):
            by_contract[key] = row
    return list(by_contract.values())


def _position_legs(node: dict[str, Any]) -> list[dict[str, Any]]:
    for key in _LEG_LIST_KEYS:
        legs = node.get(key)
        if isinstance(legs, list):
            rows = [leg for leg in legs if isinstance(leg, dict)]
            if rows:
                return rows
    return []


def _leg_with_parent(parent: dict[str, Any], leg: dict[str, Any]) -> dict[str, Any]:
    """Copy parent quantity, cost, and underlying onto a leg that omits them."""
    merged = dict(leg)
    instrument = merged.get("instrument")
    if isinstance(instrument, dict):
        for key, value in instrument.items():
            if merged.get(key) in (None, "") and value not in (None, ""):
                merged[key] = value
    leg_qty = _first_qty(merged)
    parent_qty = _first_qty(parent)
    if (leg_qty is None or leg_qty <= 0) and parent_qty is not None and parent_qty > 0:
        merged["quantity"] = parent_qty
    if _position_cost(merged) is None:
        cost = _position_cost(parent)
        if cost is not None:
            merged["cost_price"] = cost
    if _position_mark(merged) is None:
        mark = _position_mark(parent)
        if mark is not None:
            merged["last_price"] = mark
    if not str(merged.get("underlying_symbol") or "").strip():
        parent_symbol = str(parent.get("underlying_symbol") or parent.get("symbol") or "")
        if parent_symbol and parse_occ_contract(parent_symbol) is None:
            merged.setdefault("underlying_symbol", parent_symbol)
    return merged


def _first_qty(node: dict[str, Any]) -> float | None:
    for key in ("quantity", "position", "position_quantity", "qty"):
        parsed = _coerce_qty(node.get(key))
        if parsed is not None:
            return parsed
    return None


_STRIKE_KEYS = (
    "option_exercise_price",
    "optionExercisePrice",
    "exercise_price",
    "exercisePrice",
    "strike_price",
    "strikePrice",
    "strike",
)


def _position_strike(node: dict[str, Any]) -> float | None:
    for key in _STRIKE_KEYS:
        parsed = _coerce_qty(node.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _position_cost(node: dict[str, Any]) -> float | None:
    for key in _COST_KEYS:
        parsed = _coerce_qty(node.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


# Bid is the premium we can sell. Last/mark fill in when the book has no bid.
_MARK_KEY_GROUPS = (
    ("bid", "bid_price", "bidPrice"),
    ("mark", "market_price", "marketPrice"),
    ("mid",),
    ("last_price", "lastPrice", "last", "trade_price", "tradePrice"),
)


def _position_mark(node: dict[str, Any]) -> float | None:
    """Live option premium on a positions row. Bid wins over last."""
    for keys in _MARK_KEY_GROUPS:
        for key in keys:
            parsed = _coerce_qty(node.get(key))
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _position_identity(node: dict[str, Any]) -> dict[str, Any] | None:
    contract = None
    for key in ("contract", "symbol", "instrument_id", "option_symbol", "occ_symbol"):
        parsed = parse_occ_contract(str(node.get(key) or ""))
        if parsed:
            contract = parsed
            break
    option_type = str(node.get("option_type") or node.get("optionType") or "").upper()
    right = str(node.get("right") or "").upper()
    side = str(node.get("side") or "").upper()
    if right in {"CALL", "C"} or option_type == "CALL" or side in {"CALL", "C"}:
        right = "C"
    elif right in {"PUT", "P"} or option_type == "PUT" or side in {"PUT", "P"}:
        right = "P"
    else:
        right = ""
    expiry = str(
        node.get("option_expire_date")
        or node.get("optionExpireDate")
        or node.get("expiry")
        or node.get("expire_date")
        or ""
    )[:10]
    strike = _position_strike(node)
    qty = _first_qty(node)
    instrument = str(node.get("instrument_type") or node.get("instrumentType") or "").upper()
    looks_option = bool(contract) or instrument == "OPTION" or option_type in {"CALL", "PUT"} or right in {"C", "P"}
    if not looks_option or qty is None or qty <= 0:
        return None
    cost = _position_cost(node)
    if contract:
        row = {
            "contract": contract["contract"],
            "symbol": contract["symbol"],
            "right": contract["right"],
            "expiry": contract["expiry"],
            "strike": contract["strike"],
            "quantity": qty,
        }
    elif not right or strike is None or len(expiry) < 10:
        return None
    else:
        symbol = str(node.get("underlying_symbol") or node.get("symbol") or "").upper()
        if not symbol or parse_occ_contract(symbol):
            return None
        contract = format_occ_contract(symbol, expiry, right, float(strike)) or ""
        row = {
            "contract": contract,
            "symbol": symbol,
            "right": right,
            "expiry": expiry,
            "strike": float(strike),
            "quantity": qty,
        }
    if cost is not None:
        row["cost"] = cost
    mark = _position_mark(node)
    if mark is not None:
        row["mark"] = mark
    for key in ("position_id", "positionId"):
        token = str(node.get(key) or "").strip()
        if token:
            row["position_id"] = token
            break
    return row


def apply_occ_identity(intent: WebullOrderIntent) -> WebullOrderIntent:
    """Overwrite right/expiry/strike from the OCC symbol when one is present.

    Production rows kept a CALL right and a later expiry on the signal while
    the OCC contract was the put or the earlier date. The contract is the
    order.
    """
    ident = parse_occ_contract(intent.contract)
    if not ident:
        return intent
    intent.symbol = str(ident["symbol"])
    intent.right = str(ident["right"])
    intent.expiry = str(ident["expiry"])
    intent.strike = float(ident["strike"])
    intent.contract = str(ident["contract"])
    return intent


def build_option_place_order(intent: WebullOrderIntent) -> dict[str, Any]:
    """Single-leg option body for ``order_v2.place_option``.

    Official options orders use ``POST /trading/orders/place`` with
    ``instrument_type=OPTION`` and a ``legs`` array. The same body is sent to
    ``api.sandbox.webull.com`` (paper) or ``api.webull.com`` (production).
    Options do not accept MARKET orders — a positive limit is required.
    Sell-side time in force is DAY. When ``intent.contract`` is an OCC symbol
    it wins over a conflicting right or expiry on the row.
    """
    apply_occ_identity(intent)
    side = "BUY" if str(intent.action or "").upper() == "BUY" else "SELL"
    if str(intent.action or "").upper() not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported side {intent.action}")
    symbol = str(intent.symbol or "").upper().strip()
    if not symbol:
        raise ValueError("symbol required")
    if intent.strike is None:
        raise ValueError("strike required")
    expiry = str(intent.expiry or "").strip()
    if len(expiry) < 10 or expiry[4] != "-" or expiry[7] != "-":
        raise ValueError("expiry must be YYYY-MM-DD")
    right = str(intent.right or "C").upper()
    if right in {"CALL", "C"}:
        option_type = "CALL"
    elif right in {"PUT", "P"}:
        option_type = "PUT"
    else:
        raise ValueError(f"unsupported right {intent.right}")
    try:
        limit_f = float(intent.limit_price) if intent.limit_price is not None else 0.0
    except (TypeError, ValueError):
        limit_f = 0.0
    if limit_f <= 0:
        raise ValueError("limit price required (Webull options do not accept MARKET)")
    qty = max(1, int(intent.quantity or 1))
    client_order_id = str(intent.id or "").strip()[:32]
    if not client_order_id:
        raise ValueError("client_order_id required")
    tif = "DAY" if side == "SELL" else str(intent.tif or "DAY").upper()
    if tif not in {"DAY", "GTC"}:
        tif = "DAY"
    # A sell is a close of the long we already hold. The parent intent is what
    # the US options schema documents. The leg repeats it so a sandbox that
    # ignores the parent does not treat the call as a naked short. Buys keep
    # the previous leg shape. Quantity is capped to the live long before this
    # body is built.
    position_intent = "BUY_TO_OPEN" if side == "BUY" else "SELL_TO_CLOSE"
    leg = {
        "side": side,
        "quantity": str(qty),
        "symbol": symbol,
        "strike_price": f"{float(intent.strike):.2f}",
        "option_expire_date": expiry[:10],
        "instrument_type": "OPTION",
        "option_type": option_type,
        "market": "US",
    }
    if side == "SELL":
        leg["position_intent"] = position_intent
    return {
        "client_order_id": client_order_id,
        "combo_type": "NORMAL",
        "order_type": "LIMIT",
        "quantity": str(qty),
        "limit_price": f"{limit_f:.2f}",
        "option_strategy": "SINGLE",
        "side": side,
        "time_in_force": tif,
        "entrust_type": "QTY",
        "instrument_type": "OPTION",
        "market": "US",
        "symbol": symbol,
        "position_intent": position_intent,
        "legs": [leg],
    }


def multi_leg_rejected(error: str | None) -> bool:
    """True when the sandbox refused the order because it is a multi-leg strategy.

    A price, tick, or buying-power reject is not this. Those stay rejected
    instead of turning into two naked legs.
    """
    blob = str(error or "").upper()
    if not blob:
        return False
    phrases = (
        "UNSUPPORTED STRATEGY",
        "STRATEGY_NOT_SUPPORT",
        "STRATEGY NOT SUPPORT",
        "INVALID_STRATEGY",
        "INVALID STRATEGY",
        "OPTION_STRATEGY",
        "MULTI_LEG_NOT",
        "MULTI-LEG NOT",
        "MULTI LEG NOT",
        "COMBO_NOT",
        "DOES NOT SUPPORT MULTI",
        "NOT SUPPORT MULTI",
    )
    return any(phrase in blob for phrase in phrases)


def build_vertical_spread_order(
    *,
    client_order_id: str,
    symbol: str,
    expiry: str,
    short_strike: float,
    long_strike: float,
    quantity: int = 1,
    limit_price: float,
    opening: bool = True,
    right: str = "P",
) -> dict[str, Any]:
    """One Webull vertical. Opening a credit uses side SELL and a credit limit.

    The short strike is sold to open and the long strike is bought to open.
    Closing buys the short strike back and sells the long strike. The limit
    is the net credit when opening and the net debit when closing.
    ``option_strategy`` is VERTICAL. The sandbox derives the structure from
    the legs; the field is the documented reference value.
    """
    symbol_u = str(symbol or "").upper().strip()
    if not symbol_u:
        raise ValueError("symbol required")
    if str(symbol_u) == "SPX":
        raise ValueError("SPX is not a Webull order")
    expiry_s = str(expiry or "").strip()
    if len(expiry_s) < 10 or expiry_s[4] != "-" or expiry_s[7] != "-":
        raise ValueError("expiry must be YYYY-MM-DD")
    try:
        short_k = float(short_strike)
        long_k = float(long_strike)
        limit_f = float(limit_price)
    except (TypeError, ValueError) as exc:
        raise ValueError("strikes and limit are required") from exc
    if short_k <= 0 or long_k <= 0 or abs(short_k - long_k) < 0.01:
        raise ValueError("vertical needs two different strikes")
    if limit_f <= 0:
        raise ValueError("limit price required (Webull options do not accept MARKET)")
    right_u = str(right or "P").upper()
    if right_u in {"CALL", "C"}:
        option_type = "CALL"
    elif right_u in {"PUT", "P"}:
        option_type = "PUT"
    else:
        raise ValueError(f"unsupported right {right}")
    # A put credit sells the higher strike. Keep the caller's short/long even
    # if a call credit would flip them: short is the sold strike.
    order_id = str(client_order_id or "").strip()[:32]
    if not order_id:
        raise ValueError("client_order_id required")
    qty = max(1, int(quantity or 1))
    side = "SELL" if opening else "BUY"

    def leg(strike: float, leg_side: str, intent: str) -> dict[str, Any]:
        return {
            "side": leg_side,
            "quantity": str(qty),
            "symbol": symbol_u,
            "strike_price": f"{float(strike):.2f}",
            "option_expire_date": expiry_s[:10],
            "instrument_type": "OPTION",
            "option_type": option_type,
            "market": "US",
            "position_intent": intent,
        }

    if opening:
        short_leg = leg(short_k, "SELL", "SELL_TO_OPEN")
        long_leg = leg(long_k, "BUY", "BUY_TO_OPEN")
    else:
        short_leg = leg(short_k, "BUY", "BUY_TO_CLOSE")
        long_leg = leg(long_k, "SELL", "SELL_TO_CLOSE")
    return {
        "client_order_id": order_id,
        "combo_type": "NORMAL",
        "order_type": "LIMIT",
        "quantity": str(qty),
        "limit_price": f"{limit_f:.2f}",
        "option_strategy": "VERTICAL",
        "side": side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "instrument_type": "OPTION",
        "market": "US",
        "symbol": symbol_u,
        "legs": [short_leg, long_leg],
    }


def _broker_order_id(res: Any, fallback: str) -> str:
    """Pull an order id out of an OpenAPI SDK response. Raise on HTTP errors."""
    status = getattr(res, "status_code", None)
    if status is not None and int(status) not in {200, 201}:
        text = str(getattr(res, "text", "") or "")[:300]
        raise RuntimeError(f"Webull place_option HTTP {status}: {text}")
    data: Any = res
    if hasattr(res, "json") and callable(res.json):
        data = res.json()
    if isinstance(data, dict):
        for key in ("order_id", "orderId", "client_order_id", "clientOrderId"):
            if data.get(key):
                return str(data[key])
        nested = data.get("orders") or data.get("new_orders") or data.get("data")
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            first = nested[0]
            return str(
                first.get("order_id")
                or first.get("orderId")
                or first.get("client_order_id")
                or first.get("clientOrderId")
                or fallback
            )
        return fallback
    if isinstance(data, list) and data and isinstance(data[0], dict):
        first = data[0]
        return str(first.get("order_id") or first.get("orderId") or first.get("client_order_id") or fallback)
    return str(getattr(res, "order_id", None) or fallback)


_ERR_CODE_RE = re.compile(r"Code:\s*([A-Z0-9_]+)")
_ERR_MSG_RE = re.compile(r"Msg:\s*(.+?)(?:,\s*RequestID:|$)")

# Webull says the account has nothing to close. A lower price cannot help.
NO_POSITION_CODES = frozenset(
    {
        "OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION",
        "OPENAPI_POSITION_ORDER_INTENT_MISMATCH",
        "OPENAPI_TICKER_ID_NOT_SUPPORT_NAKED_EQUTY_OPTION_WITH_NUM",
    }
)
# Sent outside 9:30-4:00 ET. Queue for the next open instead of rejecting.
OUTSIDE_HOURS_CODES = frozenset({"OPENAPI_OPTION_ONLY_SUPPORT_MARKET_IN_CORE_TIME"})


def parse_webull_error(text: Any) -> tuple[str | None, str | None]:
    """(code, plain message) from a Webull SDK error string. No request ids."""
    raw = str(text or "")
    code_m = _ERR_CODE_RE.search(raw)
    msg_m = _ERR_MSG_RE.search(raw)
    code = code_m.group(1) if code_m else None
    msg = msg_m.group(1).strip() if msg_m else None
    if msg is None and raw and code is None:
        msg = raw[:200]
    return code, msg


def reject_kind(code: str | None) -> str:
    if code in NO_POSITION_CODES:
        return "no_position"
    if code in OUTSIDE_HOURS_CODES:
        return "outside_hours"
    if code and "PRICE_STEP" in code:
        return "tick"
    return "other"


class WebullRateLimit(RuntimeError):
    """Sandbox rejected the call with HTTP 429 / TOO_MANY_REQUESTS."""


_FILLED_STATUS = {"FILLED"}
_PARTIAL_STATUS = {"PARTIAL FILLED", "PARTIALLY FILLED", "PARTIAL"}
_WORKING_STATUS = {"SUBMITTED", "PENDING", "WORKING", "NEW", "PENDING SUBMIT"}
_CANCELLED_STATUS = {"CANCELLED", "CANCELED", "FAILED", "REJECTED"}


def _norm_order_status(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw.isdigit():
        return None
    return " ".join(raw.upper().replace("-", " ").replace("_", " ").split())


def _coerce_qty(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_broker_order(payload: Any) -> dict[str, Any]:
    """Map a Webull order-detail payload to filled / partial / working / cancelled.

    A resting SUBMITTED limit is working, not a position. FILLED, or a filled
    quantity that covers the order size, is a position. PARTIAL FILLED is both:
    the filled contracts can be sold and the remainder is still resting.
    """
    status: str | None = None
    filled: float | None = None
    qty: float | None = None
    fill_price: float | None = None

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal status, filled, qty, fill_price
        if depth > 6:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if status is None:
            for key in ("order_status", "orderStatus"):
                token = _norm_order_status(node.get(key))
                if token:
                    status = token
                    break
            if status is None:
                token = _norm_order_status(node.get("status"))
                if token in _FILLED_STATUS | _PARTIAL_STATUS | _WORKING_STATUS | _CANCELLED_STATUS:
                    status = token
        if filled is None:
            for key in ("filled_quantity", "filled_qty", "filledQuantity", "filledQty"):
                parsed = _coerce_qty(node.get(key))
                if parsed is not None:
                    filled = parsed
                    break
        if qty is None:
            for key in ("total_quantity", "quantity", "qty"):
                parsed = _coerce_qty(node.get(key))
                if parsed is not None:
                    qty = parsed
                    break
        if fill_price is None:
            for key in _FILL_PRICE_KEYS:
                parsed = _coerce_qty(node.get(key))
                if parsed is not None and parsed > 0:
                    fill_price = parsed
                    break
        if status is None or filled is None or qty is None or fill_price is None:
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)

    walk(payload)
    covered = filled is not None and qty is not None and qty > 0 and filled + 1e-9 >= qty
    if covered or status in _FILLED_STATUS:
        fill_state = "filled"
    elif status in _CANCELLED_STATUS and not (filled is not None and filled > 0):
        fill_state = "cancelled"
    elif status in _PARTIAL_STATUS or (filled is not None and filled > 0):
        fill_state = "partial"
    elif status in _WORKING_STATUS:
        fill_state = "working"
    elif status in _CANCELLED_STATUS:
        fill_state = "cancelled"
    else:
        fill_state = "unknown"
    return {
        "fill_state": fill_state,
        "filled_qty": filled,
        "quantity": qty,
        "order_status": status,
        "fill_price": fill_price,
    }


def _response_status(res: Any) -> int | None:
    status = getattr(res, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _response_text(res: Any) -> str:
    return str(getattr(res, "text", "") or "")[:500]


def _looks_like_rate_limit(status: int | None, text: str | None) -> bool:
    if status == 429:
        return True
    blob = f"{text or ''}".upper()
    return "TOO_MANY_REQUESTS" in blob or "RATE_LIMIT" in blob or "RATE LIMIT" in blob or "HTTP 429" in blob


def _is_rate_limit_error(exc: BaseException) -> bool:
    return isinstance(exc, WebullRateLimit) or _looks_like_rate_limit(None, str(exc))


def _response_body(res: Any) -> Any:
    if isinstance(res, (dict, list)):
        return res
    parsed = None
    if hasattr(res, "json") and callable(res.json):
        try:
            parsed = res.json()
        except Exception:  # noqa: BLE001
            parsed = None
    if isinstance(parsed, (dict, list)):
        return parsed
    text = parsed if isinstance(parsed, str) else _response_text(res)
    if isinstance(text, str):
        stripped = text.strip()
        if stripped[:1] in "[{":
            try:
                loaded = json.loads(stripped)
            except Exception:  # noqa: BLE001
                loaded = None
            if isinstance(loaded, (dict, list)):
                return loaded
    return {"raw": _response_text(res)}


_LEDGER_LIMIT = 500
_LEDGER_OPEN = {"staged", "submitted", "deferred", "partial"}


def _ledger_qty(order: WebullOrderIntent) -> float:
    meta = order.meta or {}
    try:
        filled = float(meta.get("filled_qty"))
    except (TypeError, ValueError):
        filled = 0.0
    if filled > 0:
        return filled
    try:
        return float(order.quantity or 0)
    except (TypeError, ValueError):
        return 0.0


def _ledger_filled(order: WebullOrderIntent) -> bool:
    if order.status in {"filled", "partial"}:
        return True
    return str((order.meta or {}).get("fill_state") or "") in {"filled", "partial"}


def _ledger_qty_by(orders: list[WebullOrderIntent], action: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for order in orders:
        key = str(order.contract or "")
        if order.action == action and _ledger_filled(order) and key:
            totals[key] = totals.get(key, 0.0) + _ledger_qty(order)
    return totals


def orders_for_ledger(orders: list[WebullOrderIntent], limit: int = _LEDGER_LIMIT) -> list[WebullOrderIntent]:
    """Newest orders, plus every filled buy a kept sell still has to match.

    Edge pairs a sell with the buy on the same contract. Keeping only the
    newest 500 dropped that buy while the sell was still in the file.
    A buy with no sell yet stays too, so the exit can still find its entry.
    """
    if len(orders) <= limit:
        return list(orders)
    chosen_ids = {id(order) for order in orders[:limit]}
    chosen_ids.update(id(order) for order in orders if order.status in _LEDGER_OPEN)
    have = _ledger_qty_by([order for order in orders if id(order) in chosen_ids], "BUY")
    gap = {
        key: qty - have.get(key, 0.0)
        for key, qty in _ledger_qty_by([order for order in orders if id(order) in chosen_ids], "SELL").items()
        if qty - have.get(key, 0.0) > 1e-6
    }
    for order in reversed(orders):
        key = str(order.contract or "")
        if order.action != "BUY" or not _ledger_filled(order) or gap.get(key, 0.0) <= 1e-6:
            continue
        if id(order) in chosen_ids:
            gap[key] = gap.get(key, 0.0) - _ledger_qty(order)
            continue
        gap[key] -= min(_ledger_qty(order), gap[key])
        chosen_ids.add(id(order))
    # Still-open buys are not in the newest window once the tape is busy.
    left = _ledger_qty_by(orders, "SELL")
    for order in reversed(orders):
        if order.action != "BUY" or not _ledger_filled(order):
            continue
        key = str(order.contract or "")
        qty = _ledger_qty(order)
        cover = min(qty, left.get(key, 0.0)) if key else 0.0
        if key:
            left[key] = left.get(key, 0.0) - cover
        if qty - cover > 1e-6:
            chosen_ids.add(id(order))
    return [order for order in orders if id(order) in chosen_ids]


def _order_not_found(exc: BaseException) -> bool:
    text = str(exc).upper()
    return "ORDER NOT PRESENT" in text or "NOT_FOUND" in text or "ORDER_NOT_EXIST" in text or "NOT EXIST" in text


def _detail_order_id(payload: Any) -> str | None:
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 5 or found:
            return
        if isinstance(node, dict):
            value = node.get("order_id") or node.get("orderId")
            if value:
                found.append(str(value))
                return
            for item in node.values():
                walk(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload)
    return found[0] if found else None


def flatten_history_group(group: Any) -> list[dict[str, Any]]:
    """One row per single-leg order in a Webull order-history group."""
    if not isinstance(group, dict):
        return []
    inner = group.get("orders") if isinstance(group.get("orders"), list) else [group]
    out: list[dict[str, Any]] = []
    for order in inner:
        if not isinstance(order, dict):
            continue
        legs = order.get("legs") if isinstance(order.get("legs"), list) else []
        leg = legs[0] if legs and isinstance(legs[0], dict) else {}
        out.append(
            {
                "client_order_id": order.get("client_order_id") or group.get("client_order_id"),
                "order_id": order.get("order_id"),
                "side": str(order.get("side") or leg.get("side") or "").upper(),
                "status": order.get("status") or order.get("order_status"),
                "symbol": str(leg.get("symbol") or order.get("symbol") or "").upper(),
                "option_type": leg.get("option_type"),
                "strike": leg.get("strike_price"),
                "expiry": leg.get("option_expire_date"),
                "quantity": order.get("total_quantity") or leg.get("quantity"),
                "filled_quantity": order.get("filled_quantity"),
                "limit_price": order.get("limit_price"),
                "filled_price": order.get("filled_price"),
                "place_time_at": order.get("place_time_at"),
                "filled_time_at": order.get("filled_time_at"),
                "position_intent": order.get("position_intent"),
                "legs": len(legs),
            }
        )
    return out


def _history_status(raw: Any) -> str:
    token = _norm_order_status(raw) or ""
    if token in _FILLED_STATUS:
        return "filled"
    if token in _PARTIAL_STATUS:
        return "partial"
    if token in {"REJECTED", "FAILED"}:
        return "rejected"
    if token in _CANCELLED_STATUS:
        return "cancelled"
    if token in _WORKING_STATUS:
        return "submitted"
    return "submitted"


def _intent_from_history(row: dict[str, Any], status: str, broker: "WebullBroker") -> "WebullOrderIntent":
    side = str(row.get("side") or "").upper()
    right = "P" if str(row.get("option_type") or "").upper().startswith("P") else "C"
    strike = _coerce_qty(row.get("strike"))
    expiry = str(row.get("expiry") or "")[:10] or None
    symbol = str(row.get("symbol") or "").upper()
    contract = format_occ_contract(symbol, expiry, right, strike) if (symbol and expiry and strike is not None) else None
    placed = str(row.get("place_time_at") or "") or _now()
    qty = int(_coerce_qty(row.get("quantity")) or 1)
    intent = WebullOrderIntent(
        id=str(row.get("client_order_id")),
        desk="golden_exit" if side == "SELL" else "webull_import",
        action="SELL" if side == "SELL" else "BUY",
        symbol=symbol,
        right=right,
        contract=contract,
        expiry=expiry,
        strike=strike,
        quantity=max(1, qty),
        limit_price=_coerce_qty(row.get("limit_price")),
        fill_price=_coerce_qty(row.get("filled_price")),
        reason="restored from Webull order history (ledger had lost this order)",
        status=status,
        broker_order_id=str(row.get("order_id") or "") or None,
        created_at=placed,
        # The fill time, not the import time, so daily P&L lands on the right day.
        updated_at=str(row.get("filled_time_at") or "") or placed,
        meta={
            "source": "webull_history_import",
            "fill_state": status if status in {"filled", "partial", "cancelled", "rejected"} else "working",
            "filled_qty": _coerce_qty(row.get("filled_quantity")),
            "broker_status": _norm_order_status(row.get("status")),
            "position_intent": row.get("position_intent"),
            "paper": broker.paper,
            "sandbox": broker.sandbox,
            **({"wb_place_time_at": row.get("place_time_at")} if row.get("place_time_at") else {}),
            **({"wb_filled_time_at": row.get("filled_time_at")} if row.get("filled_time_at") else {}),
        },
    )
    return intent


def _apply_history(order: "WebullOrderIntent", row: dict[str, Any], status: str) -> bool:
    changed = False
    moved = False  # status, price, or quantity changed (not just a timestamp note)
    meta = order.meta if isinstance(order.meta, dict) else {}
    order.meta = meta
    for key, value in (("wb_place_time_at", row.get("place_time_at")), ("wb_filled_time_at", row.get("filled_time_at"))):
        if value and meta.get(key) != value:
            meta[key] = value
            changed = True
    if not order.broker_order_id and row.get("order_id"):
        order.broker_order_id = str(row.get("order_id"))
        changed = True
    current = str(order.status or "")
    final = {"filled", "cancelled", "rejected", "expired"}
    if status == "submitted" and current in final | {"partial"}:
        return changed
    if current == "expired":
        return changed
    if status != current and (current not in final or status == "filled"):
        order.status = status
        meta["fill_state"] = status if status != "submitted" else "working"
        meta["broker_status"] = _norm_order_status(row.get("status"))
        meta["status_from_webull_history"] = True
        if current == "staged":
            meta["reconciled_from"] = "staged"
        changed = moved = True
    price = _coerce_qty(row.get("filled_price"))
    if price is not None and price > 0 and status in {"filled", "partial"} and order.fill_price != price:
        order.fill_price = price
        changed = moved = True
    filled = _coerce_qty(row.get("filled_quantity"))
    if filled is not None and status in {"filled", "partial"} and meta.get("filled_qty") != filled:
        meta["filled_qty"] = filled
        changed = moved = True
    if moved:
        # Stamp Webull's fill time so the trip lands on the day it happened.
        order.updated_at = str(row.get("filled_time_at") or "") or _now()
    return changed


# Every WebullBroker instance keeps the ledger in memory. The focus scan, the
# fast desk, the portal, and the watch loop each build their own, so a save
# from a broker loaded a minute ago used to overwrite rows another broker had
# just written. That is how filled Webull sells vanished from the ledger and
# how a sent order stayed "staged". Saves now merge with the file under a
# lock, and only rows this broker changed win.
_LEDGER_LOCK = threading.RLock()


class _file_lock:
    """Advisory flock on ``<ledger>.lock`` so a scan subprocess cannot interleave."""

    def __init__(self, path: Path):
        self.path = Path(str(path) + ".lock")
        self._fh = None

    def __enter__(self) -> "_file_lock":
        try:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001
            self._fh = None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            self._fh.close()
            self._fh = None

_STATUS_RANK = {
    "staged": 0,
    "deferred": 1,
    "dry_run": 1,
    "submitted": 2,
    "partial": 3,
    "skipped": 4,
    "cancelled": 5,
    "rejected": 5,
    "filled": 6,
    "expired": 7,
}


def _row_sig(order: "WebullOrderIntent") -> str:
    try:
        return json.dumps(order.to_dict(), sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return repr(order)


def _pick_row(mine: "WebullOrderIntent", disk: "WebullOrderIntent") -> "WebullOrderIntent":
    """Both copies changed. Keep the further-along status, then the newer write.

    A row that reached Webull (broker id) never goes back to an unsent copy.
    The loser's meta keys fill gaps in the winner so peak marks survive.
    """
    def rank(order: "WebullOrderIntent") -> tuple[int, int, str]:
        sent = 1 if order.broker_order_id else 0
        return (sent, _STATUS_RANK.get(str(order.status or ""), 0), str(order.updated_at or ""))

    win, lose = (mine, disk) if rank(mine) >= rank(disk) else (disk, mine)
    merged_meta = dict(lose.meta or {})
    merged_meta.update(win.meta or {})
    win.meta = merged_meta
    if not win.broker_order_id and lose.broker_order_id:
        win.broker_order_id = lose.broker_order_id
    if (win.fill_price is None or win.fill_price <= 0) and lose.fill_price:
        win.fill_price = lose.fill_price
    return win


def _copy_into(target: "WebullOrderIntent", source: "WebullOrderIntent") -> None:
    for f in fields(WebullOrderIntent):
        value = getattr(source, f.name)
        if f.name == "meta":
            value = dict(value or {})
        setattr(target, f.name, value)


def merge_ledger_rows(
    memory: list["WebullOrderIntent"],
    disk: list["WebullOrderIntent"],
    baseline: dict[str, str],
) -> list["WebullOrderIntent"]:
    """Merge the file into this broker's rows in place.

    ``baseline`` is each row as this broker last read or wrote it. A row this
    broker did not touch takes the file copy. A row only this broker touched
    keeps its copy. A row both sides touched goes through ``_pick_row``. Rows
    only on disk are added. In-memory objects keep their identity, so a caller
    holding an intent still edits the ledger row.
    """
    by_disk = {order.id: order for order in disk}
    seen: set[str] = set()
    out: list[WebullOrderIntent] = []
    for order in memory:
        seen.add(order.id)
        other = by_disk.get(order.id)
        if other is None:
            out.append(order)
            continue
        base = baseline.get(order.id)
        mine_sig = _row_sig(order)
        if base is not None and base == mine_sig:
            _copy_into(order, other)
        elif base is not None and base == _row_sig(other):
            pass
        elif mine_sig != _row_sig(other):
            chosen = _pick_row(order, other)
            if chosen is not order:
                _copy_into(order, chosen)
        out.append(order)
    for other in disk:
        if other.id not in seen:
            out.append(other)
    out.sort(key=lambda o: str(o.created_at or ""), reverse=True)
    return out


def _parse_iso_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


class EndpointBudget:
    """Process-wide sliding-window budget per Webull endpoint.

    The paper sandbox allows about 30 calls per 60 s per endpoint per app key
    (developer.webull.com/apis/docs/rate-limits). Every broker in this process
    (scan, fast desk, fast exits, portal) shares one budget, so the loops
    cannot add up past the limit and earn 429s. An order call (place/cancel)
    waits up to a few seconds for a slot; a read (detail/positions/history)
    does not wait and is skipped until the next pass.
    """

    def __init__(self, per_min: int = 28, window_sec: float = 60.0):
        self.per_min = max(1, int(per_min))
        self.window_sec = float(window_sec)
        self._calls: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()

    def used(self, endpoint: str, now: float | None = None) -> int:
        clock = time.monotonic() if now is None else now
        with self._lock:
            rows = [t for t in self._calls.get(endpoint, []) if clock - t < self.window_sec]
            self._calls[endpoint] = rows
            return len(rows)

    def reserve(self, endpoint: str, *, max_wait: float, sleeper: Callable[[float], Any]) -> None:
        """Take a slot, waiting up to ``max_wait`` seconds. Raises WebullRateLimit otherwise.

        Raising happens before any HTTP call, so nothing reached Webull and the
        caller may retry the same client order id later.
        """
        if str(os.environ.get("WEBULL_LOCAL_RATE_LIMIT", "1")).strip().lower() in {"0", "false", "off", "no"}:
            return
        waited = 0.0
        while True:
            with self._lock:
                clock = time.monotonic()
                rows = [t for t in self._calls.get(endpoint, []) if clock - t < self.window_sec]
                if len(rows) < self.per_min:
                    rows.append(clock)
                    self._calls[endpoint] = rows
                    return
                self._calls[endpoint] = rows
                wait = self.window_sec - (clock - rows[0]) + 0.05
            if waited + wait > max_wait:
                raise WebullRateLimit(
                    f"local Webull budget: {endpoint} used {self.per_min}/{int(self.window_sec)}s — retry in {wait:.0f}s"
                )
            sleeper(max(0.0, wait))
            waited += max(0.05, wait)


def _budget_per_min() -> int:
    try:
        return max(1, int(float(os.environ.get("WEBULL_SANDBOX_CALLS_PER_MIN") or 28)))
    except ValueError:
        return 28


WEBULL_BUDGET = EndpointBudget(_budget_per_min())
ORDER_ENDPOINTS = {"place", "cancel"}
ORDER_MAX_WAIT_SEC = 5.0
# Last detail check per client order id (monotonic), shared by every broker.
_DETAIL_LAST: dict[str, float] = {}


class WebullBroker:
    """Place option orders via Webull OpenAPI, or dry-run / deep-link only."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        dry_run: bool = True,
        region: str = "us",
        sandbox: bool = True,
        paper: bool = False,
        account_id: str | None = None,
        account_id_2: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        ledger_path: str | Path | None = None,
        submit_interval_sec: float = 1.5,
        rate_limit_attempts: int = 3,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run) or not enabled
        self.region = region or "us"
        # Production placement is disabled. A caller that asks for the live host
        # still stays on the paper sandbox.
        if not (bool(sandbox) or bool(paper)):
            logger.warning(
                "Webull production host requested; forcing paper sandbox %s",
                SANDBOX_TRADE_HOST,
            )
        self.sandbox = True
        self.paper = True
        self.account_id = account_id or os.environ.get("WEBULL_ACCOUNT_ID")
        self.account_id_2 = account_id_2 or os.environ.get("WEBULL_ACCOUNT_ID_2")
        self.app_key = app_key or os.environ.get("WEBULL_APP_KEY")
        self.app_secret = app_secret or os.environ.get("WEBULL_APP_SECRET")
        self.path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orders: list[WebullOrderIntent] = []
        self._baseline: dict[str, str] = {}
        self._disk_sig: tuple[int, int] | None = None
        self._load()
        # One TradeClient for the life of this broker. A new client per submit
        # was opening a fresh HTTP stack on every golden buy.
        self._client = None
        self._trade = None
        self._order_api = None
        self.submit_interval_sec = max(0.0, float(submit_interval_sec))
        self.rate_limit_attempts = max(1, int(rate_limit_attempts))
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._last_api_at: float | None = None

    def _file_sig(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (int(st.st_mtime_ns), int(st.st_size))

    def _read_disk(self) -> list[WebullOrderIntent] | None:
        from odte_scanner.json_util import load_json_state

        raw = load_json_state(self.path)
        if not isinstance(raw, dict):
            return None
        allowed = {f.name for f in fields(WebullOrderIntent)}
        rows: list[WebullOrderIntent] = []
        try:
            for o in raw.get("orders") or []:
                if not isinstance(o, dict):
                    continue
                row = WebullOrderIntent(**{k: v for k, v in o.items() if k in allowed})
                if not isinstance(row.meta, dict):
                    row.meta = {}
                rows.append(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("webull ledger load failed: %s", exc)
            return None
        return rows

    def _reset_baseline(self) -> None:
        self._baseline = {order.id: _row_sig(order) for order in self.orders}

    def _load(self) -> None:
        with _LEDGER_LOCK:
            sig = self._file_sig()
            rows = self._read_disk()
            self.orders = rows or []
            self._disk_sig = sig
            self._reset_baseline()

    def reload(self) -> None:
        """Pull rows other brokers wrote since this one loaded. Local edits stay."""
        with _LEDGER_LOCK:
            sig = self._file_sig()
            if sig is not None and sig == self._disk_sig:
                return
            disk = self._read_disk()
            if disk is None:
                return
            self.orders = merge_ledger_rows(self.orders, disk, self._baseline)
            self._disk_sig = sig
            self._reset_baseline()

    def save(self) -> None:
        from odte_scanner.json_util import atomic_write_text

        with _LEDGER_LOCK, _file_lock(self.path):
            sig = self._file_sig()
            if sig is not None and sig != self._disk_sig:
                disk = self._read_disk()
                if disk is not None:
                    self.orders = merge_ledger_rows(self.orders, disk, self._baseline)
            # The file used to keep the newest 500 and drop the buy before its
            # sell, so the Edge tab could not match the exit. Unmatched buys stay.
            self.orders = orders_for_ledger(self.orders)
            atomic_write_text(
                self.path,
                json.dumps(
                    {
                        "updated_at": _now(),
                        "enabled": self.enabled,
                        "dry_run": self.dry_run,
                        "sandbox": self.sandbox,
                        "paper": self.paper,
                        "host": webull_trade_host(sandbox=self.sandbox),
                        "orders": [o.to_dict() for o in self.orders],
                    },
                    indent=2,
                ),
            )
            self._disk_sig = self._file_sig()
            self._reset_baseline()

    def status(self) -> dict[str, Any]:
        sdk = False
        try:
            import webull  # noqa: F401

            sdk = True
        except Exception:
            try:
                from webull.core.client import ApiClient  # noqa: F401

                sdk = True
            except Exception:
                sdk = False
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "sandbox": self.sandbox,
            "paper": self.paper,
            "host": webull_trade_host(sandbox=self.sandbox),
            "region": self.region,
            "has_app_key": bool(self.app_key),
            "has_app_secret": bool(self.app_secret),
            "has_account_id": bool(self.account_id),
            "has_account_id_2": bool(self.account_id_2),
            "sdk_available": sdk,
            "ready_submit": bool(
                self.enabled
                and not self.dry_run
                and self.app_key
                and self.app_secret
                and self.account_id
                and sdk
            ),
            "ready_paper": bool(
                self.paper
                and self.enabled
                and not self.dry_run
                and self.app_key
                and self.app_secret
                and self.account_id
                and sdk
            ),
            "ready_live": bool(
                self.enabled
                and not self.dry_run
                and not self.paper
                and self.app_key
                and self.app_secret
                and self.account_id
                and sdk
            ),
            "orders": len(self.orders),
            "submit_interval_sec": self.submit_interval_sec,
            "open_staged": sum(
                1
                for o in self.orders
                if o.status in ("staged", "dry_run", "submitted", "deferred", "partial")
            ),
            "disclaimer": (
                "Webull routing is golden-only by default: Gold Setup cards rendered "
                "gold (MAX and HIGH). Unusual-flow prints stay on the board. "
                "Paper mode uses the official sandbox host api.sandbox.webull.com "
                "(Webull OpenAPI Paper Trading) — not the production account. "
                "dry_run stays on until sandbox keys and WEBULL_PAPER_SUBMIT=1 "
                "(or enabled + dry_run false). Options can go to zero."
            ),
        }

    def stage(
        self,
        *,
        desk: str,
        action: str,
        symbol: str,
        right: str = "C",
        contract: str | None = None,
        expiry: str | None = None,
        strike: float | None = None,
        quantity: int = 1,
        limit_price: float | None = None,
        reason: str = "",
        hist_win_pct: float | None = None,
        hist_samples: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WebullOrderIntent:
        now = _now()
        intent = WebullOrderIntent(
            id=f"wb-{uuid.uuid4().hex[:10]}",
            desk=str(desk).lower(),
            action=str(action).upper(),
            symbol=str(symbol).upper(),
            right=str(right or "C").upper(),
            contract=contract,
            expiry=expiry,
            strike=float(strike) if strike is not None else None,
            quantity=max(1, int(quantity or 1)),
            limit_price=float(limit_price) if limit_price is not None else None,
            reason=(reason or "")[:320],
            hist_win_pct=hist_win_pct,
            hist_samples=hist_samples,
            status="staged",
            deep_link=webull_option_deep_link(
                symbol, expiry=expiry, strike=strike, right=right
            ),
            created_at=now,
            updated_at=now,
            meta=meta or {},
        )
        intent.meta["app_scheme"] = webull_app_scheme(symbol)
        intent.meta["paper"] = self.paper
        intent.meta["sandbox"] = self.sandbox
        intent.meta["host"] = webull_trade_host(sandbox=self.sandbox)
        self.orders.insert(0, intent)
        self.save()
        return intent

    def _retry_tick_reject(self, intent: WebullOrderIntent, exc: BaseException) -> bool:
        """Re-round a rejected limit and let submit try the same order once."""
        if (intent.meta or {}).get("tick_retried"):
            return False
        if not is_tick_reject(exc):
            return False
        new_limit = rereound_after_reject(float(intent.limit_price or 0), side=str(intent.action or "BUY"))
        if new_limit is None or intent.limit_price is None:
            return False
        if abs(float(new_limit) - float(intent.limit_price)) < 0.001:
            return False
        intent.meta["tick_retried"] = True
        intent.meta["limit_before_tick_retry"] = intent.limit_price
        intent.limit_price = new_limit
        intent.status = "staged"
        intent.error = None
        return True

    def _snap_limit(self, intent: WebullOrderIntent) -> None:
        """Write a legal option tick back onto the intent before it is sent."""
        if intent.limit_price is None:
            return
        side = str(intent.action or "BUY").upper()
        cap = None
        if side == "BUY":
            try:
                raw_cap = (intent.meta or {}).get("ask_cap")
                cap = float(raw_cap) if raw_cap not in (None, "") else None
            except (TypeError, ValueError):
                cap = None
            if cap is not None and cap <= 0:
                cap = None
        snapped = normalize_option_limit(float(intent.limit_price), side=side, cap=cap)
        if snapped > 0:
            intent.limit_price = snapped

    def submit(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        """Submit staged intent — dry-run unless fully armed."""
        intent.updated_at = _now()
        self._snap_limit(intent)
        if not self.enabled:
            # Still log dry-run previews so Journal history fills while learning the bridge
            if self.dry_run:
                intent.status = "dry_run"
                intent.error = None
                intent.meta["note"] = (
                    "Preview only (live_trading.enabled is false). "
                    "Paper submits need sandbox keys plus WEBULL_PAPER_SUBMIT=1 "
                    "(or enabled: true and dry_run: false). "
                    "WEBULL_LIVE cannot place a production order."
                    if self.paper
                    else (
                        "Preview only (live_trading.enabled is false). "
                        "Set enabled: true + dry_run: true to arm the bridge without live fills; "
                        "then dry_run: false + API keys for OpenAPI submit."
                    )
                )
                self.save()
                logger.info(
                    "WEBULL PREVIEW %s %s %s [%s] (enabled=false)",
                    intent.action,
                    intent.symbol,
                    intent.contract or intent.strike,
                    intent.desk,
                )
                return intent
            intent.status = "skipped"
            intent.error = "live_trading.enabled is false"
            self.save()
            return intent

        if self.dry_run or not (self.app_key and self.app_secret and self.account_id):
            intent.status = "dry_run"
            intent.error = None
            intent.meta["note"] = (
                "Dry-run only. Paper mode targets "
                f"{webull_trade_host(sandbox=True)} (sandbox account). "
                "Set sandbox WEBULL_APP_KEY / WEBULL_APP_SECRET / WEBULL_ACCOUNT_ID "
                "(or WEBULL_PAPER_ACCOUNT_ID) and WEBULL_PAPER_SUBMIT=1 to submit. "
                "Nothing is sent while dry_run is on."
                if self.paper
                else (
                    "Dry-run only. Open deep_link in Webull to trade manually, "
                    "or set WEBULL_APP_KEY / WEBULL_APP_SECRET / WEBULL_ACCOUNT_ID "
                    "and dry_run: false for OpenAPI submission."
                )
            )
            self.save()
            logger.info(
                "WEBULL DRY-RUN %s %s %s %s x%s @ %s [%s]",
                intent.action,
                intent.symbol,
                intent.right,
                intent.contract or intent.strike,
                intent.quantity,
                intent.limit_price,
                intent.desk,
            )
            return intent

        held = self._hold_outside_session(intent)
        if held is not None:
            return held

        try:
            broker_id = self._place_via_openapi(intent)
            intent.meta["acked_at"] = _now()
            intent.status = "submitted"
            intent.broker_order_id = broker_id
            intent.error = None
            intent.meta["fill_state"] = "working"
            intent.meta["queued_for_open"] = False
        except WebullRateLimit as exc:
            # Same client_order_id is still the only order. The next sync
            # retries this row instead of staging a second one.
            logger.warning("Webull rate limit; deferring %s: %s", intent.id, exc)
            intent.status = "deferred"
            intent.error = str(exc)[:500]
            intent.meta["fill_state"] = "deferred"
            intent.meta["retryable"] = True
        except Exception as exc:  # noqa: BLE001
            if self._retry_tick_reject(intent, exc):
                return self.submit(intent)
            code, msg = parse_webull_error(exc)
            kind = reject_kind(code)
            intent.meta["reject_code"] = code
            intent.meta["reject_reason"] = (msg or str(exc))[:240]
            intent.meta["reject_kind"] = kind
            if kind == "outside_hours" and str(intent.action or "").upper() == "SELL":
                # Webull only takes option orders 9:30-4:00 ET. Same client
                # order id goes out at the next open, unless the contract expires.
                logger.warning("Webull outside hours; queueing %s: %s", intent.id, msg)
                intent.status = "deferred"
                intent.error = f"queued until 09:30 ET — {msg or code}"[:500]
                intent.meta["fill_state"] = "deferred"
                intent.meta["queued_for_open"] = True
                intent.meta["retryable"] = True
                self.save()
                return intent
            logger.warning("Webull submit rejected %s %s: %s", intent.id, code, msg)
            intent.status = "rejected"
            intent.error = str(exc)[:500]
            intent.meta["fill_state"] = "rejected"
            # A broker reject such as OPENAPI_ETF_OPTION_CAN_NOT_TRADING is not
            # a 429. Do not stage a fresh order for the same contract next sync.
            intent.meta["retryable"] = False
        self.save()
        return intent

    def _hold_outside_session(self, intent: WebullOrderIntent) -> WebullOrderIntent | None:
        """Skip the HTTP call outside 09:30–16:15 ET, and do not buy after 15:45."""
        from odte_scanner.signals.hold_rules import (
            before_option_session,
            option_session_open,
            past_odte_flatten,
        )

        now = getattr(self, "_session_now", None)
        action = str(intent.action or "").upper()
        if action == "BUY" and past_odte_flatten(now):
            return self._stop_unsent_buy(intent, "cancelled at 15:45 ET")
        if option_session_open(now):
            return None
        if before_option_session(now) or action != "BUY":
            return self._queue_until_open(intent)
        return self._stop_unsent_buy(intent, "not submitted outside 09:30–16:15 ET")

    def _queue_until_open(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        intent.status = "deferred"
        intent.error = "queued until 09:30 ET"
        intent.meta["queued_for_open"] = True
        intent.meta["retryable"] = True
        intent.meta["fill_state"] = "deferred"
        self.save()
        return intent

    def _stop_unsent_buy(self, intent: WebullOrderIntent, reason: str) -> WebullOrderIntent:
        intent.meta["retryable"] = False
        intent.meta["queued_for_open"] = False
        if not intent.broker_order_id and intent.status not in {"filled", "partial", "cancelled"}:
            intent.status = "cancelled"
            intent.meta["fill_state"] = "cancelled"
            intent.error = reason
        self.save()
        return intent

    def retry_deferred(self, limit: int | None = None) -> list[WebullOrderIntent]:
        """Resubmit rows that exhausted 429 retries, keeping their client_order_id."""
        out: list[WebullOrderIntent] = []
        for intent in list(self.orders):
            if intent.status != "deferred":
                continue
            if limit is not None and len(out) >= limit:
                break
            out.append(self.submit(intent))
        return out

    # ---- stuck-order recovery -------------------------------------------------

    @staticmethod
    def _age_sec(stamp: str | None, now: datetime | None = None) -> float | None:
        if not stamp:
            return None
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment - when).total_seconds()

    def _live(self) -> bool:
        return bool(self.enabled and not self.dry_run and self.app_key and self.app_secret and self.account_id)

    def stale_staged(self, now: datetime | None = None, min_age_sec: float = 20.0) -> list[WebullOrderIntent]:
        """Rows saved as staged, never confirmed sent, older than ``min_age_sec``."""
        # Ages use the wall clock. Rows are stamped in real time even when a
        # caller passes a desk clock for session rules.
        del now
        out: list[WebullOrderIntent] = []
        for intent in self.orders:
            if intent.status != "staged" or intent.broker_order_id:
                continue
            age = self._age_sec(intent.created_at)
            if age is None or age >= float(min_age_sec):
                out.append(intent)
        return out

    def reconcile_staged(
        self,
        now: datetime | None = None,
        *,
        min_age_sec: float = 20.0,
        resend_buy_max_age_sec: float = 90.0,
    ) -> list[dict[str, Any]]:
        """Recover rows stuck in "staged".

        Webull is asked first (order detail by client order id), so nothing is
        sent twice. A row Webull knows takes Webull's status and fill. A buy
        Webull never saw is resent once while it is still young; an older buy,
        and every unsent sell, is cleared so a fresh exit can be priced off the
        live position on the next pass. A failed lookup leaves the row for the
        next cycle.
        """
        actions: list[dict[str, Any]] = []
        stale = self.stale_staged(now, min_age_sec)
        if not stale:
            return actions
        live = self._live()
        for intent in stale:
            age = self._age_sec(intent.created_at) or 0.0
            if not live:
                self._clear_unsent(intent, "stuck staged row cleared (bridge not live)")
                actions.append({"id": intent.id, "action": "cleared", "age_sec": round(age, 1)})
                continue
            try:
                payload = self._fetch_order_detail(intent)
            except Exception as exc:  # noqa: BLE001
                if not _order_not_found(exc):
                    logger.warning("staged reconcile lookup failed for %s: %s", intent.id, exc)
                    continue
                payload = None
            classified = classify_broker_order(payload) if payload is not None else None
            if classified is not None and (classified.get("order_status") or classified.get("fill_state") != "unknown"):
                intent.broker_order_id = _detail_order_id(payload) or intent.broker_order_id or intent.id
                intent.status = "submitted"
                intent.error = None
                intent.meta["reconciled_from"] = "staged"
                self._apply_fill(intent, classified)
                intent.updated_at = _now()
                actions.append({"id": intent.id, "action": "adopted", "status": intent.status, "age_sec": round(age, 1)})
                continue
            if intent.action == "BUY" and age <= float(resend_buy_max_age_sec):
                intent.meta["resent_by_reconciler"] = True
                self.submit(intent)
                actions.append({"id": intent.id, "action": "resent", "status": intent.status, "age_sec": round(age, 1)})
                continue
            self._clear_unsent(intent, "never reached Webull — cleared by reconciler")
            actions.append({"id": intent.id, "action": "cleared", "age_sec": round(age, 1)})
        self.save()
        return actions

    _INHERIT_META = (
        "golden_kind", "gold_exit", "diamond_flat_ct", "sniper_flat_ct", "afternoon_flat_ct",
        "exit_plan", "take_profit_pct", "stop_loss_pct", "account_id", "signal_ask", "original_qty",
    )

    def _inherit_desk(self, buy: WebullOrderIntent) -> None:
        """A restored buy is usually a chase replacement. Take the desk and exit
        plan of the ledger buy for the same contract so exits manage it.

        Only a sibling on the same Chicago day and the same account counts
        (weekly contracts repeat across days and desks). Prefer the latest one
        placed at or before the restored order (a chase original); else the
        earliest one after it. A BUY_TO_CLOSE (closing a short, e.g. a lunch
        credit leg) never inherits a long desk.
        """
        if str((buy.meta or {}).get("position_intent") or "").upper() == "BUY_TO_CLOSE":
            return
        placed = _parse_iso_utc(buy.created_at)
        if placed is None:
            return
        from odte_scanner.time_cst import CT

        day = placed.astimezone(CT).date()
        before: tuple[datetime, WebullOrderIntent] | None = None
        after: tuple[datetime, WebullOrderIntent] | None = None
        primary = str(self.account_id or "")
        for order in self.orders:
            if order is buy or order.action != "BUY" or order.contract != buy.contract:
                continue
            if order.desk in {"webull_import", ""}:
                continue
            acct = str((order.meta or {}).get("account_id") or "")
            if acct and primary and acct != primary:
                continue  # second account: history import reads the primary only
            at = _parse_iso_utc(order.created_at)
            if at is None or at.astimezone(CT).date() != day:
                continue
            if at <= placed + timedelta(seconds=5):
                if before is None or at > before[0]:
                    before = (at, order)
            elif after is None or at < after[0]:
                after = (at, order)
        chosen = before or after
        if chosen is None:
            return
        best = chosen[1]
        buy.desk = best.desk
        for key in self._INHERIT_META:
            if key in (best.meta or {}) and key not in buy.meta:
                buy.meta[key] = best.meta[key]
        buy.meta["desk_from"] = best.id

    def _link_sell_to_buy(self, sell: WebullOrderIntent) -> None:
        """Point a restored sell at the filled buy it closed (same contract, earlier)."""
        best = None
        for order in self.orders:
            if order.action != "BUY" or order.contract != sell.contract:
                continue
            if order.status not in {"filled", "partial"}:
                continue
            if str(order.created_at or "") > str(sell.created_at or ""):
                continue
            if best is None or str(order.created_at or "") > str(best.created_at or ""):
                best = order
        if best is None:
            return
        sell.meta["closes_order_id"] = best.id
        if best.fill_price:
            sell.meta["entry_price"] = best.fill_price
        sell.meta["entry_at"] = best.created_at
        kind = str((best.meta or {}).get("golden_kind") or best.desk or "")
        tier = {"diamond": "diamond", "sniper": "sniper", "afternoon_puts": "afternoon"}.get(kind)
        if tier is None and kind in {"sure_winner_gold", "sure_winner"}:
            tier = "gold"
        if tier:
            sell.meta["exit_tier"] = tier
        if best.desk == "challenge":
            sell.desk = "challenge"

    def _clear_unsent(self, intent: WebullOrderIntent, reason: str) -> None:
        intent.status = "cancelled"
        intent.error = reason
        intent.meta["fill_state"] = "cancelled"
        intent.meta["never_sent"] = True
        intent.meta["retryable"] = False
        intent.meta["cancel_reason"] = reason
        intent.updated_at = _now()

    def order_history(self, start_date: str, end_date: str, *, max_pages: int = 20) -> list[dict[str, Any]] | None:
        """Flattened Webull order history rows (single legs). None on failure."""
        if not self._live():
            return None
        try:
            api = self._trade_api()
        except Exception as exc:  # noqa: BLE001
            logger.warning("order history unavailable: %s", exc)
            return None
        meth = getattr(api, "get_order_history", None)
        if not callable(meth):
            return None
        rows: list[dict[str, Any]] = []
        last: str | None = None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {"page_size": 100, "start_date": start_date, "end_date": end_date}
            if last:
                kwargs["last_client_order_id"] = last
            try:
                res = self._call_with_backoff(lambda: meth(self.account_id, **kwargs), "history")
            except Exception as exc:  # noqa: BLE001
                logger.warning("order history failed: %s", exc)
                return None if not rows else rows
            body = _response_body(res)
            groups = body if isinstance(body, list) else (body.get("orders") or body.get("data") or [])
            if not isinstance(groups, list) or not groups:
                break
            for group in groups:
                rows.extend(flatten_history_group(group))
            nxt = str((groups[-1] or {}).get("client_order_id") or "")
            if not nxt or nxt == last or len(groups) < 100:
                break
            last = nxt
        return rows

    def import_history(self, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Make the ledger agree with Webull's own order history.

        Webull paper fills are the source of truth. A ``wb-`` order Webull has
        that the ledger lost is added back. A ledger row whose status disagrees
        with Webull (for example "staged" while Webull filled it) takes
        Webull's status, fill price, and fill time.
        """
        summary = {"added": 0, "updated": 0, "checked": 0}
        if not rows:
            return summary
        by_id = {o.id: o for o in self.orders}
        changed = False
        touched: list[WebullOrderIntent] = []
        for row in rows:
            cid = str(row.get("client_order_id") or "")
            if not cid.startswith("wb-"):
                continue
            summary["checked"] += 1
            status = _history_status(row.get("status"))
            existing = by_id.get(cid)
            if existing is None:
                intent = _intent_from_history(row, status, self)
                if intent.action == "SELL":
                    self._link_sell_to_buy(intent)
                else:
                    self._inherit_desk(intent)
                self.orders.append(intent)
                by_id[cid] = intent
                summary["added"] += 1
                changed = True
                touched.append(intent)
                continue
            if _apply_history(existing, row, status):
                summary["updated"] += 1
                changed = True
                touched.append(existing)
        for intent in touched:
            if intent.action == "SELL" and intent.status in {"filled", "partial"}:
                try:
                    from odte_scanner.trading.expiry import void_for_real_sell

                    if void_for_real_sell(self.orders, intent):
                        summary["voided_expiry"] = summary.get("voided_expiry", 0) + 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("expiry void check failed: %s", exc)
                try:
                    from odte_scanner.edge.round_trips import record_sell_fill

                    record_sell_fill(intent, self.orders)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("paper round trip was not recorded: %s", exc)
        if changed:
            self.orders.sort(key=lambda o: str(o.created_at or ""), reverse=True)
            self.save()
        return summary

    def _assert_sandbox(self) -> None:
        if not self.sandbox or not self.paper:
            raise RuntimeError(
                "Refusing production Webull host api.webull.com. "
                "This bridge only places paper-sandbox orders."
            )
        host = webull_trade_host(sandbox=True)
        if host != SANDBOX_TRADE_HOST:
            raise RuntimeError(f"Refusing non-sandbox Webull host {host}")

    def _throttle(self) -> None:
        """Space sandbox calls. The first call in a process does not wait."""
        interval = float(self.submit_interval_sec or 0)
        now = self._clock()
        if interval > 0 and self._last_api_at is not None:
            wait = interval - (now - self._last_api_at)
            if wait > 0:
                self._sleeper(wait)
                now = self._clock()
        self._last_api_at = now

    def _call_with_backoff(self, fn: Callable[[], Any], endpoint: str = "other") -> Any:
        """Run fn. On 429, sleep and retry the same request. Never mint a new order id.

        Each attempt first takes a slot in the shared per-endpoint budget.
        """
        last: BaseException | None = None
        attempts = self.rate_limit_attempts
        max_wait = ORDER_MAX_WAIT_SEC if endpoint in ORDER_ENDPOINTS else 0.0
        for attempt in range(attempts):
            WEBULL_BUDGET.reserve(endpoint, max_wait=max_wait, sleeper=self._sleeper)
            self._throttle()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                if not _is_rate_limit_error(exc):
                    raise
                last = exc
                if attempt + 1 >= attempts:
                    break
                # A real 429 backs off at least 1.5 s, even with fast spacing.
                delay = max(1.5, float(self.submit_interval_sec or 0)) * (2 ** attempt)
                self._sleeper(delay)
        raise WebullRateLimit(str(last)[:500] if last else "TOO_MANY_REQUESTS")

    def _trade_api(self) -> Any:
        """SDK order API, built once and reused for place, detail, and cancel."""
        if self._order_api is not None:
            self._assert_sandbox()
            return self._order_api
        self._assert_sandbox()
        self._order_api = self._build_trade_api()
        return self._order_api

    def _build_trade_api(self) -> Any:
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "webull-openapi-python-sdk not installed. "
                "pip install webull-openapi-python-sdk"
            ) from exc
        from odte_scanner.trading.log_redact import install_webull_log_redaction, remember_secret

        remember_secret(self.app_key, self.app_secret)
        install_webull_log_redaction()
        host = SANDBOX_TRADE_HOST
        client = ApiClient(self.app_key, self.app_secret, self.region)
        client.add_endpoint(self.region, host)
        # Without these the SDK adds a DEBUG stdout handler and a
        # webull_trade_sdk.log file handler on every TradeClient (one per
        # broker), and those print request headers. SDK records go to our
        # logging instead, through the redaction filter.
        client._stream_logger_set = True
        client._file_logger_set = True
        trade = TradeClient(client)
        install_webull_log_redaction()  # loggers the SDK created while initialising
        order_api = getattr(trade, "order_v2", None) or getattr(trade, "order", None)
        if order_api is None:
            raise RuntimeError("TradeClient has no order API in this SDK version")
        self._client = client
        self._trade = trade
        return order_api

    def _place_via_openapi(self, intent: WebullOrderIntent) -> str:
        """Place a single-leg option order on the paper sandbox host only."""
        self._assert_sandbox()

        def _send() -> str:
            # Stamped after the local budget and spacing, right before the HTTP call.
            sent_at = _now()
            intent.meta["sent_at"] = sent_at
            intent.meta.setdefault("first_sent_at", sent_at)
            return self._post_option(intent)

        return self._call_with_backoff(_send, "place")

    def _order_account(self, intent: WebullOrderIntent | None = None) -> str:
        """Sandbox account for this order. Per-order meta overrides the primary id."""
        if intent is not None:
            chosen = str((intent.meta or {}).get("account_id") or "").strip()
            if chosen:
                return chosen
        return str(self.account_id or "")

    def lookup_account_id(self, account_number: str = CASH_ACCOUNT_NUMBER) -> str | None:
        """Resolve an account number to Webull's internal id via get_account_list.

        Returns None when sandbox credentials are missing or the number is not
        in the list. Does not print the app key or secret.
        """
        if not (self.app_key and self.app_secret):
            return None
        self._assert_sandbox()
        self._trade_api()
        trade = getattr(self, "_trade", None)
        account = getattr(trade, "account_v2", None) or getattr(trade, "account", None)
        meth = getattr(account, "get_account_list", None) if account is not None else None
        if not callable(meth):
            return None
        res = meth()
        payload: Any
        if isinstance(res, (dict, list)):
            payload = res
        elif hasattr(res, "json"):
            try:
                payload = res.json()
            except Exception:  # noqa: BLE001
                payload = getattr(res, "text", None)
        else:
            payload = getattr(res, "text", None)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                return None
        return find_account_id(payload, account_number)

    def _post_option(self, intent: WebullOrderIntent) -> str:
        custom = (intent.meta or {}).get("openapi_body")
        if isinstance(custom, dict) and custom.get("legs"):
            order = dict(custom)
            if intent.limit_price:
                order["limit_price"] = f"{float(intent.limit_price):.2f}"
        else:
            order = build_option_place_order(intent)
        account_id = self._order_account(intent)
        if not account_id or account_id.upper() == CASH_ACCOUNT_NUMBER:
            raise RuntimeError(
                "Webull account_id missing or still the account number. "
                "Use the internal id from get_account_list."
            )
        intent.meta["host"] = SANDBOX_TRADE_HOST
        intent.meta["openapi_order"] = {
            "symbol": order["symbol"],
            "side": order["side"],
            "option_type": order["legs"][0]["option_type"],
            "strike_price": order["legs"][0]["strike_price"],
            "option_expire_date": order["legs"][0]["option_expire_date"],
        }
        order_api = self._trade_api()
        res = None
        for meth_name in ("place_option", "place_option_order", "place_order"):
            meth = getattr(order_api, meth_name, None)
            if not callable(meth):
                continue
            if meth_name == "place_option":
                res = meth(account_id, [order])
            else:
                res = meth(account_id=account_id, new_orders=[order])
            break
        if res is None:
            raise RuntimeError(
                "Installed Webull SDK does not expose place_option. "
                "Update webull-openapi-python-sdk or use deep_link manual trading."
            )
        status = _response_status(res)
        text = _response_text(res)
        if _looks_like_rate_limit(status, text):
            raise WebullRateLimit(f"Webull HTTP {status or 429}: {text}"[:500])
        return _broker_order_id(res, intent.id)

    def list_option_positions(self, account_id: str | None = None) -> list[dict[str, Any]] | None:
        """Long option positions on the sandbox account.

        ``None`` means the positions call failed and the caller should trust
        the ledger. An empty list means the sandbox account has no option
        position. Dry-run never calls the network.
        """
        if self.dry_run or not self.enabled:
            return []
        acct = str(account_id or self.account_id or "")
        if not (self.app_key and self.app_secret and acct):
            return None
        try:
            payload = self._fetch_positions(acct)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webull positions failed: %s", exc)
            return None
        return parse_option_positions(payload)

    def _fetch_positions(self, account_id: str | None = None) -> Any:
        def _once() -> Any:
            self._assert_sandbox()
            self._trade_api()
            trade = getattr(self, "_trade", None)
            account = getattr(trade, "account_v2", None) or getattr(trade, "account", None)
            meth = None
            if account is not None:
                meth = getattr(account, "get_account_position", None) or getattr(
                    account, "get_account_positions", None
                )
            if not callable(meth):
                raise RuntimeError("Installed Webull SDK does not expose get_account_position")
            res = meth(str(account_id or self.account_id or ""))
            status = _response_status(res)
            text = _response_text(res)
            if _looks_like_rate_limit(status, text):
                raise WebullRateLimit(f"Webull HTTP {status or 429}: {text}"[:500])
            if status is not None and status not in {200, 201}:
                raise RuntimeError(f"Webull positions HTTP {status}: {text}")
            return _response_body(res)

        return self._call_with_backoff(_once, "positions")

    def refresh_working_buys(self) -> list[WebullOrderIntent]:
        """Update submitted/partial buys from order detail. Dry-run rows stay simulated."""
        return self._refresh_working("BUY")

    def refresh_working_sells(self) -> list[WebullOrderIntent]:
        """Update submitted/partial sells. A resting sell is not a finished exit."""
        return self._refresh_working("SELL")

    def _refresh_working(self, action: str) -> list[WebullOrderIntent]:
        """One detail call per working order, oldest-checked first.

        Detail calls share a ~30/min sandbox budget across every loop. When the
        budget is spent the rest wait for the next pass instead of 429-ing, and
        the rotation means the same orders are not the ones always skipped.
        """
        if self.dry_run or not self.enabled:
            return []
        out: list[WebullOrderIntent] = []
        rows = [o for o in self.orders if o.action == action and o.status in {"submitted", "partial"}]
        rows.sort(key=lambda o: _DETAIL_LAST.get(o.id, 0.0))
        for intent in rows:
            if WEBULL_BUDGET.used("detail") >= WEBULL_BUDGET.per_min and self._live():
                out.append(intent)  # stays working; checked first next pass
                continue
            if len(_DETAIL_LAST) > 5000:
                _DETAIL_LAST.clear()
            _DETAIL_LAST[intent.id] = time.monotonic()
            out.append(self.refresh_fill(intent))
        return out

    def refresh_fill(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        """Record whether a live limit actually filled. Failures stay working, not sold."""
        if intent.status == "dry_run":
            return intent
        if intent.status not in {"submitted", "partial"}:
            return intent
        if self.dry_run or not self.enabled:
            return intent
        try:
            payload = self._fetch_order_detail(intent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webull order detail failed for %s: %s", intent.id, exc)
            intent.meta["fill_state"] = intent.meta.get("fill_state") or "working"
            intent.updated_at = _now()
            self.save()
            return intent
        self._apply_fill(intent, classify_broker_order(payload))
        intent.updated_at = _now()
        self.save()
        return intent

    def _apply_fill(self, intent: WebullOrderIntent, classified: dict[str, Any]) -> None:
        state = str(classified.get("fill_state") or "unknown")
        if state == "unknown":
            state = "working"
        intent.meta["fill_state"] = state
        intent.meta["filled_qty"] = classified.get("filled_qty")
        intent.meta["broker_status"] = classified.get("order_status")
        price = _coerce_qty(classified.get("fill_price"))
        if price is not None and price > 0:
            intent.fill_price = price
        if state in {"filled", "partial"} and not intent.meta.get("filled_at"):
            # When this process first saw the fill. Webull's own fill time
            # (wb_filled_time_at, from order history) is preferred when present.
            intent.meta["filled_at"] = _now()
        if state == "filled":
            intent.status = "filled"
        elif state == "partial":
            intent.status = "partial"
        elif state == "cancelled":
            broker_status = str(classified.get("order_status") or "")
            intent.status = "rejected" if broker_status == "REJECTED" else "cancelled"
        else:
            intent.status = "submitted"
        if intent.action == "SELL" and intent.status in {"filled", "partial"}:
            try:
                from odte_scanner.edge.round_trips import record_sell_fill

                record_sell_fill(intent, self.orders)
            except Exception as exc:  # noqa: BLE001
                logger.warning("paper round trip was not recorded: %s", exc)

    def _fetch_order_detail(self, intent: WebullOrderIntent) -> Any:
        def _once() -> Any:
            self._assert_sandbox()
            api = self._trade_api()
            meth = getattr(api, "get_order_detail", None) or getattr(api, "query_order_detail", None)
            if not callable(meth):
                raise RuntimeError("Installed Webull SDK does not expose get_order_detail")
            res = meth(self._order_account(intent), intent.id)
            status = _response_status(res)
            text = _response_text(res)
            if _looks_like_rate_limit(status, text):
                raise WebullRateLimit(f"Webull HTTP {status or 429}: {text}"[:500])
            if status is not None and status not in {200, 201}:
                raise RuntimeError(f"Webull order detail HTTP {status}: {text}")
            return _response_body(res)

        return self._call_with_backoff(_once, "detail")

    def cancel(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        """Cancel a resting limit. Dry-run rows are cancelled locally. Sandbox only."""
        intent.updated_at = _now()
        if intent.status in {"filled", "cancelled"}:
            return intent
        # A deferred or staged row never received a broker id. Cancelling it
        # on the wire fails and would leave it eligible for another submit.
        if intent.status in {"deferred", "staged"} and not intent.broker_order_id:
            intent.status = "cancelled"
            intent.meta["fill_state"] = "cancelled"
            intent.meta["retryable"] = False
            intent.error = None
            self.save()
            return intent
        live = (
            self.enabled
            and not self.dry_run
            and intent.status != "dry_run"
            and self.app_key
            and self.app_secret
            and self.account_id
        )
        if not live:
            intent.status = "cancelled"
            intent.meta["fill_state"] = "cancelled"
            intent.error = None
            self.save()
            return intent
        try:
            self._cancel_via_openapi(intent)
            intent.status = "cancelled"
            intent.meta["fill_state"] = "cancelled"
            intent.error = None
        except WebullRateLimit as exc:
            logger.warning("Webull cancel rate limit for %s: %s", intent.id, exc)
            intent.error = str(exc)[:500]
            intent.meta["cancel_deferred"] = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Webull cancel failed: %s", exc)
            intent.error = str(exc)[:500]
        self.save()
        return intent

    def _cancel_via_openapi(self, intent: WebullOrderIntent) -> None:
        def _once() -> None:
            self._assert_sandbox()
            api = self._trade_api()
            meth = getattr(api, "cancel_option", None)
            if not callable(meth):
                raise RuntimeError("Installed Webull SDK does not expose cancel_option")
            res = meth(self._order_account(intent), intent.id)
            status = _response_status(res)
            text = _response_text(res)
            if _looks_like_rate_limit(status, text):
                raise WebullRateLimit(f"Webull HTTP {status or 429}: {text}"[:500])
            if status is not None and status not in {200, 201}:
                raise RuntimeError(f"Webull cancel_option HTTP {status}: {text}")

        self._call_with_backoff(_once, "cancel")

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self.orders[:limit]]

    def activity(self, limit: int = 40) -> dict[str, Any]:
        """BUY/SELL history summary for the Journal Webull panel."""
        from odte_scanner.trading.labels import activity_premium_label

        rows = []
        for order in self.orders[:limit]:
            row = order.to_dict()
            row["premium_label"] = activity_premium_label(row)
            rows.append(row)
        buys = [r for r in rows if str(r.get("action") or "").upper() == "BUY"]
        sells = [r for r in rows if str(r.get("action") or "").upper() == "SELL"]
        liveish = [
            r
            for r in rows
            if str(r.get("status") or "") in {"dry_run", "submitted", "staged", "deferred", "partial"}
        ]
        skipped = [r for r in rows if str(r.get("status") or "") == "skipped"]
        by_status: dict[str, int] = {}
        for r in rows:
            st = str(r.get("status") or "unknown")
            by_status[st] = by_status.get(st, 0) + 1
        return {
            "orders": rows,
            "buys": buys,
            "sells": sells,
            "active_or_dry_run": liveish,
            "skipped": skipped,
            "counts": {
                "all": len(rows),
                "buys": len(buys),
                "sells": len(sells),
                "dry_run_or_submitted": len(liveish),
                "skipped": len(skipped),
                **{f"status_{k}": v for k, v in by_status.items()},
            },
            "how_to_verify": [
                "1. Run a live Flask host (not GitHub Pages) — Pages is read-only.",
                "1. Only Gold Setup cards rendered gold (MAX and HIGH) are staged. Golden flow stays on the board.",
                "2. Keep dry_run: true until sandbox keys are set. Nothing is sent by default.",
                "3. Paper account: generate keys under Webull Developer → Paper Trading / Sandbox, then set WEBULL_APP_KEY, WEBULL_APP_SECRET, and WEBULL_ACCOUNT_ID (or WEBULL_PAPER_ACCOUNT_ID). Host is api.sandbox.webull.com.",
                "4. Submit to that paper account with WEBULL_PAPER_SUBMIT=1 (keys required). WEBULL_LIVE cannot place a production order.",
                "5. Statuses: dry_run = logged only; submitted = accepted but not necessarily filled; filled = a position to sell; deferred = 429, same client_order_id retried next sync; cancelled = unfilled limit pulled. Confirm the sandbox account, not the live brokerage account.",
            ],
        }
