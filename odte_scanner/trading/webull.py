"""Webull broker bridge — deep links + optional OpenAPI order placement.

Safety defaults:
  • dry_run=True unless live_trading.enabled and credentials are present
  • Never stores passwords in the repo — use env WEBULL_APP_KEY / WEBULL_APP_SECRET
  • 100% hist-win gate is enforced by AutoTrader (historical filter, not a promise)

Official SDK: pip install webull-openapi-python-sdk
Docs: https://developer.webull.com/apis/docs/sdk.md
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


class WebullBroker:
    """Place option orders via Webull OpenAPI, or dry-run / deep-link only."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        dry_run: bool = True,
        region: str = "us",
        sandbox: bool = True,
        account_id: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        ledger_path: str | Path | None = None,
    ):
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run) or not enabled
        self.region = region or "us"
        self.sandbox = bool(sandbox)
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
            "region": self.region,
            "has_app_key": bool(self.app_key),
            "has_app_secret": bool(self.app_secret),
            "has_account_id": bool(self.account_id),
            "sdk_available": sdk,
            "ready_live": bool(
                self.enabled
                and not self.dry_run
                and self.app_key
                and self.app_secret
                and self.account_id
                and sdk
            ),
            "orders": len(self.orders),
            "open_staged": sum(1 for o in self.orders if o.status in ("staged", "dry_run", "submitted")),
            "disclaimer": (
                "Auto-trade uses a 100% hist-win gate as a filter on past quality signals — "
                "it does NOT guarantee future wins. Options can go to zero. Paper/dry-run "
                "until you set credentials and live_trading.enabled."
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
        self.orders.insert(0, intent)
        self.save()
        return intent

    def submit(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        """Submit staged intent — dry-run unless fully armed."""
        intent.updated_at = _now()
        if not self.enabled:
            intent.status = "skipped"
            intent.error = "live_trading.enabled is false"
            self.save()
            return intent

        if self.dry_run or not (self.app_key and self.app_secret and self.account_id):
            intent.status = "dry_run"
            intent.error = None
            intent.meta["note"] = (
                "Dry-run only. Open deep_link in Webull to trade manually, "
                "or set WEBULL_APP_KEY / WEBULL_APP_SECRET / WEBULL_ACCOUNT_ID "
                "and dry_run: false for OpenAPI submission."
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
        """Place option order through official OpenAPI SDK when installed."""
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "webull-openapi-python-sdk not installed. "
                "pip install webull-openapi-python-sdk"
            ) from exc

        client = ApiClient(self.app_key, self.app_secret, self.region)
        if self.sandbox:
            client.add_endpoint(self.region, "api.sandbox.webull.com")
        trade = TradeClient(client)

        # SDK method names evolve — try common option order entry points.
        side = "BUY" if intent.action == "BUY" else "SELL"
        qty = intent.quantity
        payload = {
            "symbol": intent.symbol,
            "option_symbol": intent.contract,
            "expiry": intent.expiry,
            "strike": intent.strike,
            "right": "CALL" if intent.right == "C" else "PUT",
            "side": side,
            "quantity": qty,
            "order_type": intent.order_type,
            "time_in_force": intent.tif,
            "limit_price": intent.limit_price,
            "account_id": self.account_id,
            "client_order_id": intent.id,
        }

        # Prefer a dedicated options order API if present
        order_api = getattr(trade, "order", None) or getattr(trade, "order_v2", None)
        if order_api is None:
            raise RuntimeError("TradeClient has no order API in this SDK version")

        for meth_name in (
            "place_option_order",
            "place_order_option",
            "create_option_order",
            "place_order",
        ):
            meth = getattr(order_api, meth_name, None)
            if callable(meth):
                res = meth(**{k: v for k, v in payload.items() if v is not None})
                # Normalize response
                if hasattr(res, "json"):
                    data = res.json() if callable(res.json) else res.json
                    if isinstance(data, dict):
                        return str(
                            data.get("orderId")
                            or data.get("order_id")
                            or data.get("clientOrderId")
                            or intent.id
                        )
                if isinstance(res, dict):
                    return str(res.get("orderId") or res.get("order_id") or intent.id)
                return str(getattr(res, "order_id", None) or intent.id)

        raise RuntimeError(
            "Installed Webull SDK does not expose a known option order method. "
            "Update webull-openapi-python-sdk or use deep_link manual trading."
        )

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self.orders[:limit]]
