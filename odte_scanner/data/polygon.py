"""Polygon / Massive market-data client (option + equity marks).

Env (either works — Massive keys are accepted under the Polygon name):
  POLYGON_API_KEY   — preferred
  MASSIVE_API_KEY   — alias (Cursor signup often lands on Massive)

Docs: https://polygon.io/docs/options/getting-started
Massive is the current Polygon options/quotes vendor brand; same key role for us.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE = "https://api.polygon.io"


def api_key_from_env() -> str | None:
    for name in ("POLYGON_API_KEY", "MASSIVE_API_KEY"):
        tok = (os.environ.get(name) or "").strip()
        if tok:
            return tok
    return None


def configured() -> bool:
    return bool(api_key_from_env())


def status() -> dict[str, Any]:
    key = api_key_from_env()
    src = "polygon"
    if (os.environ.get("MASSIVE_API_KEY") or "").strip() and not (
        os.environ.get("POLYGON_API_KEY") or ""
    ).strip():
        src = "massive"
    return {
        "configured": bool(key),
        "token_len": len(key) if key else 0,
        "base": BASE,
        "source": src,
        "endpoints": ["stocks/snapshot", "options/snapshot", "last/trade"],
    }


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def occ_symbol(
    underlying: str,
    expiry: str,
    strike: float,
    right: str = "call",
) -> str:
    """Build OCC option ticker without O: prefix, e.g. AAPL240119C00150000."""
    root = str(underlying).upper().replace(".", "")
    exp = str(expiry).replace("-", "")[:8]
    if len(exp) == 8:
        yy, mm, dd = exp[2:4], exp[4:6], exp[6:8]
        yymmdd = f"{yy}{mm}{dd}"
    else:
        yymmdd = exp[-6:]
    cp = "P" if str(right).lower().startswith("p") else "C"
    # strike * 1000, 8 digits
    k = int(round(float(strike) * 1000))
    return f"{root}{yymmdd}{cp}{k:08d}"


def _get(path: str, *, params: dict[str, Any] | None = None, timeout: float = 12.0) -> dict[str, Any]:
    key = api_key_from_env()
    if not key:
        return {"ok": False, "configured": False, "skipped": True, "error": "POLYGON_API_KEY not set"}
    try:
        q = dict(params or {})
        q["apiKey"] = key
        r = requests.get(f"{BASE}{path}", params=q, timeout=timeout)
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "error": "unauthorized (check POLYGON_API_KEY / MASSIVE_API_KEY)",
                "status_code": 401,
            }
        if r.status_code == 403:
            return {
                "ok": False,
                "configured": True,
                "error": "forbidden — plan may lack options entitlement",
                "status_code": 403,
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        return {"ok": True, "configured": True, "payload": payload}
    except Exception as exc:  # noqa: BLE001
        logger.warning("polygon GET %s failed: %s", path, exc)
        return {"ok": False, "configured": True, "error": str(exc)}


def fetch_equity_quote(symbol: str, *, timeout: float = 10.0) -> dict[str, Any] | None:
    """Best-effort equity last/prev from snapshot, then last-trade / daily aggs."""
    sym = str(symbol).upper()
    res = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}", timeout=timeout)
    if res.get("ok"):
        ticker = ((res.get("payload") or {}).get("ticker") or {})
        day = ticker.get("day") or {}
        prev = ticker.get("prevDay") or {}
        last_trade = ticker.get("lastTrade") or {}
        last = _as_float(last_trade.get("p") or day.get("c") or prev.get("c"))
        prev_close = _as_float(prev.get("c") or day.get("c") or last)
        if last > 0:
            return {
                "symbol": sym,
                "last": last,
                "prevclose": prev_close,
                "open": _as_float(day.get("o")),
                "high": _as_float(day.get("h")),
                "low": _as_float(day.get("l")),
                "close": _as_float(day.get("c")),
                "volume": int(_as_float(day.get("v"))),
                "change": last - prev_close if prev_close else 0.0,
                "source": "polygon",
            }

    # Free / limited plans: last trade
    lt = _get(f"/v2/last/trade/{sym}", timeout=timeout)
    if lt.get("ok"):
        results = (lt.get("payload") or {}).get("results") or {}
        last = _as_float(results.get("p"))
        if last > 0:
            return {
                "symbol": sym,
                "last": last,
                "prevclose": last,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "close": last,
                "volume": int(_as_float(results.get("s"))),
                "change": 0.0,
                "source": "polygon",
            }

    # Daily bars (often available on starter plans)
    aggs = _get(
        f"/v2/aggs/ticker/{sym}/prev",
        params={"adjusted": "true"},
        timeout=timeout,
    )
    if aggs.get("ok"):
        rows = (aggs.get("payload") or {}).get("results") or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        last = _as_float(row.get("c"))
        if last > 0:
            return {
                "symbol": sym,
                "last": last,
                "prevclose": last,
                "open": _as_float(row.get("o")),
                "high": _as_float(row.get("h")),
                "low": _as_float(row.get("l")),
                "close": last,
                "volume": int(_as_float(row.get("v"))),
                "change": 0.0,
                "source": "polygon",
            }
    return None


def probe(*, timeout: float = 10.0) -> dict[str, Any]:
    """Smoke-test key with SPY — snapshot, last trade, or prev daily."""
    st = status()
    if not st.get("configured"):
        return {**st, "ok": False, "error": "POLYGON_API_KEY not set"}
    q = fetch_equity_quote("SPY", timeout=timeout)
    ok = bool(q and q.get("last"))
    err = None
    if not ok:
        # Surface first API error for debugging plan entitlements
        snap = _get("/v2/snapshot/locale/us/markets/stocks/tickers/SPY", timeout=timeout)
        err = snap.get("error") or "SPY quote empty — check plan entitlement (stocks/options)"
    return {
        **st,
        "ok": ok,
        "smoke_last": (q or {}).get("last"),
        "error": err,
        "confidence_boost": "equity+option snapshots" if ok else None,
    }


def fetch_option_quote(
    *,
    symbol: str,
    expiry: str,
    strike: float,
    right: str = "call",
    contract: str | None = None,
    timeout: float = 12.0,
) -> dict[str, Any] | None:
    """Option snapshot bid/ask/last via Polygon OCC ticker."""
    if not api_key_from_env():
        return None
    occ = str(contract or "").upper().lstrip("O:")
    if not occ:
        occ = occ_symbol(symbol, expiry, strike, right)
    # Snapshot: /v3/snapshot/options/{underlying}/{optionContract}
    und = str(symbol).upper()
    res = _get(f"/v3/snapshot/options/{und}/{occ}", timeout=timeout)
    if not res.get("ok"):
        # Fallback last trade
        lt = _get(f"/v2/last/trade/O:{occ}", timeout=timeout)
        if not lt.get("ok"):
            return None
        results = (lt.get("payload") or {}).get("results") or {}
        last = _as_float(results.get("p"))
        if last <= 0:
            return None
        return {
            "symbol": occ,
            "bid": last * 0.95,
            "ask": last,
            "last": last,
            "volume": int(_as_float(results.get("s"))),
            "open_interest": 0,
            "expiry": expiry,
            "strike": float(strike),
            "right": "put" if str(right).lower().startswith("p") else "call",
            "underlying": und,
            "source": "polygon",
        }

    results = (res.get("payload") or {}).get("results") or {}
    # results may be dict or list
    row = results[0] if isinstance(results, list) and results else results
    if not isinstance(row, dict):
        return None
    day = row.get("day") or {}
    quote = row.get("last_quote") or {}
    trade = row.get("last_trade") or {}
    details = row.get("details") or {}
    greeks = row.get("greeks") or {}
    bid = _as_float(quote.get("bid") or quote.get("b"))
    ask = _as_float(quote.get("ask") or quote.get("a"))
    last = _as_float(trade.get("price") or trade.get("p") or day.get("close") or day.get("c"))
    if ask <= 0 and last > 0:
        ask = last
    if bid <= 0 and last > 0:
        bid = last * 0.95
    if ask <= 0 and bid <= 0 and last <= 0:
        return None
    strike_out = _as_float(details.get("strike_price") or strike)
    return {
        "symbol": str(details.get("ticker") or occ).lstrip("O:"),
        "bid": bid,
        "ask": ask,
        "last": last,
        "volume": int(_as_float(day.get("volume") or day.get("v"))),
        "open_interest": int(_as_float(row.get("open_interest"))),
        "expiry": str(details.get("expiration_date") or expiry),
        "strike": strike_out,
        "right": "put" if str(right).lower().startswith("p") else "call",
        "underlying": und,
        "iv": _as_float(row.get("implied_volatility")),
        "greeks": greeks if isinstance(greeks, dict) else None,
        "source": "polygon",
    }
