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
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

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
    when the payload has one. Strike, expiry, and put/call often live on
    ``legs`` while quantity and cost sit on the parent. Short or zero
    quantities are dropped. An OCC symbol on the row wins over a conflicting
    right or expiry field.
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
            for leg in legs:
                ident = _position_identity(_leg_with_parent(node, leg))
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


def _position_cost(node: dict[str, Any]) -> float | None:
    for key in _COST_KEYS:
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
    strike = _coerce_qty(node.get("strike_price") if node.get("strike_price") is not None else node.get("strike"))
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
        row = {
            "contract": "",
            "symbol": symbol,
            "right": right,
            "expiry": expiry,
            "strike": float(strike),
            "quantity": qty,
        }
    if cost is not None:
        row["cost"] = cost
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
        "position_intent": "BUY_TO_OPEN" if side == "BUY" else "SELL_TO_CLOSE",
        "legs": [leg],
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
    if hasattr(res, "json") and callable(res.json):
        try:
            return res.json()
        except Exception:  # noqa: BLE001
            pass
    return {"raw": _response_text(res)}


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

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            allowed = {f.name for f in fields(WebullOrderIntent)}
            self.orders = []
            for o in raw.get("orders") or []:
                if not isinstance(o, dict):
                    continue
                self.orders.append(WebullOrderIntent(**{k: v for k, v in o.items() if k in allowed}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("webull ledger load failed: %s", exc)
            self.orders = []

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "updated_at": _now(),
                    "enabled": self.enabled,
                    "dry_run": self.dry_run,
                    "sandbox": self.sandbox,
                    "paper": self.paper,
                    "host": webull_trade_host(sandbox=self.sandbox),
                    "orders": [o.to_dict() for o in self.orders[:500]],
                },
                indent=2,
            )
        )

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
                "Webull routing is golden-only by default: Sure Winner cards rendered "
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

    def submit(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        """Submit staged intent — dry-run unless fully armed."""
        intent.updated_at = _now()
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
            logger.exception("Webull submit failed: %s", exc)
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

    def _call_with_backoff(self, fn: Callable[[], Any]) -> Any:
        """Run fn. On 429, sleep and retry the same request. Never mint a new order id."""
        last: BaseException | None = None
        attempts = self.rate_limit_attempts
        for attempt in range(attempts):
            self._throttle()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                if not _is_rate_limit_error(exc):
                    raise
                last = exc
                if attempt + 1 >= attempts:
                    break
                delay = float(self.submit_interval_sec or 1.5) * (2 ** attempt)
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
        host = SANDBOX_TRADE_HOST
        client = ApiClient(self.app_key, self.app_secret, self.region)
        client.add_endpoint(self.region, host)
        trade = TradeClient(client)
        order_api = getattr(trade, "order_v2", None) or getattr(trade, "order", None)
        if order_api is None:
            raise RuntimeError("TradeClient has no order API in this SDK version")
        self._client = client
        self._trade = trade
        return order_api

    def _place_via_openapi(self, intent: WebullOrderIntent) -> str:
        """Place a single-leg option order on the paper sandbox host only."""
        self._assert_sandbox()
        return self._call_with_backoff(lambda: self._post_option(intent))

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

        return self._call_with_backoff(_once)

    def refresh_working_buys(self) -> list[WebullOrderIntent]:
        """Update submitted/partial buys from order detail. Dry-run rows stay simulated."""
        if self.dry_run or not self.enabled:
            return []
        out: list[WebullOrderIntent] = []
        for intent in list(self.orders):
            if intent.action != "BUY" or intent.status not in {"submitted", "partial"}:
                continue
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
        if state == "filled":
            intent.status = "filled"
        elif state == "partial":
            intent.status = "partial"
        elif state == "cancelled":
            intent.status = "cancelled"
        else:
            intent.status = "submitted"

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

        return self._call_with_backoff(_once)

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

        self._call_with_backoff(_once)

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self.orders[:limit]]

    def activity(self, limit: int = 40) -> dict[str, Any]:
        """BUY/SELL history summary for the Journal Webull panel."""
        rows = [o.to_dict() for o in self.orders[:limit]]
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
                "1. Only Sure Winner cards rendered gold (MAX and HIGH) are staged. Golden flow stays on the board.",
                "2. Keep dry_run: true until sandbox keys are set. Nothing is sent by default.",
                "3. Paper account: generate keys under Webull Developer → Paper Trading / Sandbox, then set WEBULL_APP_KEY, WEBULL_APP_SECRET, and WEBULL_ACCOUNT_ID (or WEBULL_PAPER_ACCOUNT_ID). Host is api.sandbox.webull.com.",
                "4. Submit to that paper account with WEBULL_PAPER_SUBMIT=1 (keys required). WEBULL_LIVE cannot place a production order.",
                "5. Statuses: dry_run = logged only; submitted = accepted but not necessarily filled; filled = a position to sell; deferred = 429, same client_order_id retried next sync; cancelled = unfilled limit pulled. Confirm the sandbox account, not the live brokerage account.",
            ],
        }
