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
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "outputs" / "webull_orders.json"

# Official Trading API hosts. Sandbox is the paper-trading environment
# ("Using OpenAPI service in Paper Trading" → Sandbox Trading). It is isolated
# from the production brokerage account. Same option order API on both hosts.
SANDBOX_TRADE_HOST = "api.sandbox.webull.com"
PRODUCTION_TRADE_HOST = "api.webull.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    order_type: str = "LIMIT"  # LIMIT | MARKET
    tif: str = "DAY"
    reason: str = ""
    hist_win_pct: float | None = None
    hist_samples: int | None = None
    status: str = "staged"  # staged | dry_run | submitted | rejected | skipped
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


def build_option_place_order(intent: WebullOrderIntent) -> dict[str, Any]:
    """Single-leg option body for ``order_v2.place_option``.

    Official options orders use ``POST /trading/orders/place`` with
    ``instrument_type=OPTION`` and a ``legs`` array. The same body is sent to
    ``api.sandbox.webull.com`` (paper) or ``api.webull.com`` (production).
    Options do not accept MARKET orders — a positive limit is required.
    Sell-side time in force is DAY.
    """
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
        app_key: str | None = None,
        app_secret: str | None = None,
        ledger_path: str | Path | None = None,
    ):
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run) or not enabled
        self.region = region or "us"
        self.sandbox = bool(sandbox) or bool(paper)
        self.paper = bool(paper) or self.sandbox
        self.account_id = account_id or os.environ.get("WEBULL_ACCOUNT_ID")
        self.app_key = app_key or os.environ.get("WEBULL_APP_KEY")
        self.app_secret = app_secret or os.environ.get("WEBULL_APP_SECRET")
        self.path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orders: list[WebullOrderIntent] = []
        self._load()
        self._client = None

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
            "open_staged": sum(1 for o in self.orders if o.status in ("staged", "dry_run", "submitted")),
            "disclaimer": (
                "Webull routing is golden-only by default: Sure Winner gold (MAX) and "
                "Golden unusual-flow prints. Other board rows never route. "
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
                    "WEBULL_LIVE=1 is production and is ignored while paper mode is on."
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

        try:
            broker_id = self._place_via_openapi(intent)
            intent.status = "submitted"
            intent.broker_order_id = broker_id
            intent.error = None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Webull submit failed: %s", exc)
            intent.status = "rejected"
            intent.error = str(exc)[:500]
        self.save()
        return intent

    def _place_via_openapi(self, intent: WebullOrderIntent) -> str:
        """Place a single-leg option order on the paper sandbox or production host."""
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "webull-openapi-python-sdk not installed. "
                "pip install webull-openapi-python-sdk"
            ) from exc

        order = build_option_place_order(intent)
        host = webull_trade_host(sandbox=self.sandbox)
        intent.meta["host"] = host
        intent.meta["openapi_order"] = {
            "symbol": order["symbol"],
            "side": order["side"],
            "option_type": order["legs"][0]["option_type"],
            "strike_price": order["legs"][0]["strike_price"],
            "option_expire_date": order["legs"][0]["option_expire_date"],
        }
        client = ApiClient(self.app_key, self.app_secret, self.region)
        client.add_endpoint(self.region, host)
        trade = TradeClient(client)

        order_api = getattr(trade, "order_v2", None) or getattr(trade, "order", None)
        if order_api is None:
            raise RuntimeError("TradeClient has no order API in this SDK version")

        res = None
        for meth_name in ("place_option", "place_option_order", "place_order"):
            meth = getattr(order_api, meth_name, None)
            if not callable(meth):
                continue
            if meth_name == "place_option":
                res = meth(self.account_id, [order])
            else:
                res = meth(account_id=self.account_id, new_orders=[order])
            break
        if res is None:
            raise RuntimeError(
                "Installed Webull SDK does not expose place_option. "
                "Update webull-openapi-python-sdk or use deep_link manual trading."
            )
        return _broker_order_id(res, intent.id)

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
            if str(r.get("status") or "") in {"dry_run", "submitted", "staged"}
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
                "1. Only Sure Winner gold (MAX) and Golden unusual-flow prints are staged. Other rows stay on the board.",
                "2. Keep dry_run: true until sandbox keys are set. Nothing is sent by default.",
                "3. Paper account: generate keys under Webull Developer → Paper Trading / Sandbox, then set WEBULL_APP_KEY, WEBULL_APP_SECRET, and WEBULL_ACCOUNT_ID (or WEBULL_PAPER_ACCOUNT_ID). Host is api.sandbox.webull.com.",
                "4. Submit to that paper account with WEBULL_PAPER_SUBMIT=1 (keys required). Do not set WEBULL_LIVE — that is the production host and is ignored while paper mode is on.",
                "5. Statuses: dry_run = logged only; submitted = accepted by OpenAPI; skipped = gate/off. Confirm the sandbox account, not the live brokerage account.",
            ],
        }
