"""Tradier brokerage market-data client (option marks + chains).

Env:
  TRADIER_ACCESS_TOKEN  — required (production preferred for live marks)
  TRADIER_ACCOUNT_ID    — optional (orders later)
  TRADIER_SANDBOX       — ``1``/``true`` → sandbox (15m delayed); default production

Docs: https://docs.tradier.com/
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

PROD_BASE = "https://api.tradier.com/v1"
SANDBOX_BASE = "https://sandbox.tradier.com/v1"


def access_token_from_env() -> str | None:
    tok = (os.environ.get("TRADIER_ACCESS_TOKEN") or "").strip()
    return tok or None


def use_sandbox() -> bool:
    return (os.environ.get("TRADIER_SANDBOX") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "sandbox",
    }


def _base_url() -> str:
    return SANDBOX_BASE if use_sandbox() else PROD_BASE


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def status() -> dict[str, Any]:
    tok = access_token_from_env()
    return {
        "configured": bool(tok),
        "sandbox": use_sandbox(),
        "base": _base_url(),
        "account_id_set": bool((os.environ.get("TRADIER_ACCOUNT_ID") or "").strip()),
        "token_len": len(tok) if tok else 0,
        "source": "tradier",
    }


def fetch_quotes(
    symbols: list[str],
    *,
    token: str | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """GET /markets/quotes for equity or OCC option symbols."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "quotes": {},
        }
    syms = [str(s).strip() for s in symbols if str(s).strip()]
    if not syms:
        return {"ok": True, "configured": True, "quotes": {}}
    try:
        r = requests.get(
            f"{_base_url()}/markets/quotes",
            headers=_headers(key),
            params={"symbols": ",".join(syms), "greeks": "false"},
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "error": "unauthorized (check TRADIER_ACCESS_TOKEN)",
                "status_code": 401,
                "quotes": {},
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        qblock = (payload.get("quotes") or {}) if isinstance(payload, dict) else {}
        raw = qblock.get("quote")
        rows: list[dict[str, Any]]
        if isinstance(raw, list):
            rows = [x for x in raw if isinstance(x, dict)]
        elif isinstance(raw, dict):
            rows = [raw]
        else:
            rows = []
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            out[sym] = {
                "symbol": sym,
                "bid": _as_float(row.get("bid")),
                "ask": _as_float(row.get("ask")),
                "last": _as_float(row.get("last")),
                "volume": int(_as_float(row.get("volume"))),
                "open_interest": int(_as_float(row.get("open_interest"))),
                "underlying": row.get("underlying"),
                "description": row.get("description"),
                "source": "tradier",
            }
        return {
            "ok": True,
            "configured": True,
            "sandbox": use_sandbox(),
            "quotes": out,
            "n": len(out),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier quotes failed: %s", exc)
        return {
            "ok": False,
            "configured": True,
            "error": str(exc),
            "quotes": {},
        }


def fetch_option_chain(
    symbol: str,
    expiry: str,
    *,
    token: str | None = None,
    greeks: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /markets/options/chains for one underlying + expiration."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "options": [],
        }
    try:
        r = requests.get(
            f"{_base_url()}/markets/options/chains",
            headers=_headers(key),
            params={
                "symbol": str(symbol).upper(),
                "expiration": str(expiry),
                "greeks": "true" if greeks else "false",
            },
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "error": "unauthorized",
                "status_code": 401,
                "options": [],
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        block = (payload.get("options") or {}) if isinstance(payload, dict) else {}
        raw = block.get("option")
        if isinstance(raw, list):
            opts = [x for x in raw if isinstance(x, dict)]
        elif isinstance(raw, dict):
            opts = [raw]
        else:
            opts = []
        return {
            "ok": True,
            "configured": True,
            "sandbox": use_sandbox(),
            "symbol": str(symbol).upper(),
            "expiry": str(expiry),
            "options": opts,
            "n": len(opts),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier chain failed %s %s: %s", symbol, expiry, exc)
        return {
            "ok": False,
            "configured": True,
            "error": str(exc),
            "options": [],
        }


def fetch_option_quote(
    *,
    symbol: str,
    expiry: str,
    strike: float,
    right: str = "call",
    contract: str | None = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """Best-effort Tradier mark for one contract (OCC quote or chain strike match)."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return None

    # Prefer OCC symbol quote when known
    if contract:
        q = fetch_quotes([str(contract)], token=key, timeout=timeout)
        row = (q.get("quotes") or {}).get(str(contract).upper())
        if q.get("ok") and row and (row.get("ask") or row.get("bid") or row.get("last")):
            return {
                **row,
                "expiry": expiry,
                "strike": float(strike),
                "right": "call" if str(right).lower().startswith("c") else "put",
                "underlying": symbol,
            }

    chain = fetch_option_chain(symbol, expiry, token=key, timeout=timeout)
    if not chain.get("ok"):
        return None
    want_put = str(right).lower().startswith("p")
    best = None
    best_dist = 1e18
    for opt in chain.get("options") or []:
        opt_type = str(opt.get("option_type") or opt.get("type") or "").lower()
        is_put = opt_type in {"put", "p"}
        if is_put != want_put:
            continue
        try:
            k = float(opt.get("strike"))
        except (TypeError, ValueError):
            continue
        dist = abs(k - float(strike))
        if dist < best_dist:
            best_dist = dist
            best = opt
    if not best:
        return None
    return {
        "symbol": str(best.get("symbol") or ""),
        "bid": _as_float(best.get("bid")),
        "ask": _as_float(best.get("ask")),
        "last": _as_float(best.get("last")),
        "volume": int(_as_float(best.get("volume"))),
        "open_interest": int(_as_float(best.get("open_interest"))),
        "expiry": expiry,
        "strike": _as_float(best.get("strike")),
        "right": "put" if want_put else "call",
        "underlying": symbol,
        "source": "tradier",
    }
