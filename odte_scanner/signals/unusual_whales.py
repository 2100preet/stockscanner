"""Unusual Whales options-flow client (Bearer API key).

Env: ``UNUSUAL_WHALES_API_KEY`` — never commit the key; set as a GitHub
Actions secret and pass it into the Pages workflow.

Docs: https://api.unusualwhales.com/docs
Skill: https://unusualwhales.com/skill.md
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.unusualwhales.com"
FLOW_ALERTS = f"{BASE_URL}/api/option-trades/flow-alerts"
MARKET_TIDE = f"{BASE_URL}/api/market/market-tide"
DARKPOOL_RECENT = f"{BASE_URL}/api/darkpool/recent"
NET_PREM_TMPL = f"{BASE_URL}/api/stock/{{ticker}}/net-prem-ticks"
CLIENT_API_ID = "100001"


def api_key_from_env() -> str | None:
    key = (os.environ.get("UNUSUAL_WHALES_API_KEY") or "").strip()
    return key or None


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "UW-CLIENT-API-ID": CLIENT_API_ID,
    }


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _normalize_ticker(raw: Any) -> str:
    sym = str(raw or "").upper().strip()
    # Index weeklies like SPXW → SPX for sleeve matching
    if sym.endswith("W") and len(sym) >= 4 and sym[:-1] in {"SPX", "NDX", "RUT", "VIX"}:
        return sym[:-1]
    return sym


def fetch_flow_alerts(
    *,
    api_key: str | None = None,
    limit: int = 80,
    min_premium: float = 50_000.0,
    is_call: bool | None = None,
    is_put: bool | None = None,
    ticker_symbol: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """GET /api/option-trades/flow-alerts — returns {ok, data, ...}."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "source": "unusual_whales",
            "data": [],
            "error": "UNUSUAL_WHALES_API_KEY not set",
        }

    params: dict[str, Any] = {
        "limit": int(limit),
        "min_premium": float(min_premium),
    }
    if is_call is True:
        params["is_call"] = "true"
    if is_put is True:
        params["is_put"] = "true"
    if ticker_symbol:
        params["ticker_symbol"] = str(ticker_symbol).upper()

    try:
        r = requests.get(
            FLOW_ALERTS,
            headers=_headers(key),
            params=params,
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "source": "unusual_whales",
                "data": [],
                "error": "unauthorized (check API key)",
                "status_code": 401,
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            data = []
        return {
            "ok": True,
            "configured": True,
            "source": "unusual_whales",
            "data": data,
            "status_code": r.status_code,
            "newer_than": payload.get("newer_than") if isinstance(payload, dict) else None,
            "older_than": payload.get("older_than") if isinstance(payload, dict) else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("unusual_whales flow-alerts failed: %s", exc)
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "data": [],
            "error": str(exc),
        }


def _accumulate_side(
    rows: list[Any],
    *,
    as_puts: bool,
    prem_map: dict[str, float],
    n_map: dict[str, int],
) -> None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = _normalize_ticker(row.get("ticker") or row.get("ticker_symbol"))
        if not sym:
            continue
        prem = _as_float(row.get("total_premium") or row.get("premium"))
        prem_map[sym] = prem_map.get(sym, 0.0) + prem
        n_map[sym] = n_map.get(sym, 0) + 1
        _ = as_puts  # side fixed by caller (is_call / is_put query)


def build_uw_flow_board(
    *,
    api_key: str | None = None,
    limit: int = 80,
    min_premium: float = 50_000.0,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Aggregate call/put flow alerts into challenge-friendly leaders.

    Returns leaders shaped like Yahoo ``flow_leaders`` so existing gates can reuse them:
    ``symbol``, ``sentiment``, ``net_flow_score``, ``rank``, ``call_premium``, ``put_premium``.
    """
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "source": "unusual_whales",
            "data": [],
            "leaders": [],
            "bullish_calls": [],
            "bearish_puts": [],
            "by_symbol": {},
            "error": "UNUSUAL_WHALES_API_KEY not set",
        }

    calls = fetch_flow_alerts(
        api_key=key,
        limit=limit,
        min_premium=min_premium,
        is_call=True,
        timeout=timeout,
    )
    puts = fetch_flow_alerts(
        api_key=key,
        limit=limit,
        min_premium=min_premium,
        is_put=True,
        timeout=timeout,
    )
    if not calls.get("ok") and not puts.get("ok"):
        err = calls.get("error") or puts.get("error") or "flow-alerts failed"
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "data": [],
            "leaders": [],
            "bullish_calls": [],
            "bearish_puts": [],
            "by_symbol": {},
            "error": err,
            "status_code": calls.get("status_code") or puts.get("status_code"),
        }

    call_prem: dict[str, float] = {}
    put_prem: dict[str, float] = {}
    call_n: dict[str, int] = {}
    put_n: dict[str, int] = {}
    _accumulate_side(calls.get("data") or [], as_puts=False, prem_map=call_prem, n_map=call_n)
    _accumulate_side(puts.get("data") or [], as_puts=True, prem_map=put_prem, n_map=put_n)

    symbols = sorted(set(call_prem) | set(put_prem))
    leaders: list[dict[str, Any]] = []
    by_symbol: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        c = call_prem.get(sym, 0.0)
        p = put_prem.get(sym, 0.0)
        net = (c - p) / 1000.0  # $k premium units → score-ish
        if c > p * 1.25 and c >= min_premium:
            sentiment = "bullish"
        elif p > c * 1.25 and p >= min_premium:
            sentiment = "bearish"
        else:
            sentiment = "neutral"
        row = {
            "symbol": sym,
            "sentiment": sentiment,
            "net_flow_score": round(net, 1),
            "call_premium": round(c, 0),
            "put_premium": round(p, 0),
            "call_alerts": call_n.get(sym, 0),
            "put_alerts": put_n.get(sym, 0),
            "vol_gt_oi": True,
            "top_tier": "unusual" if max(c, p) >= 250_000 else "aggressive",
            "source": "unusual_whales",
        }
        by_symbol[sym] = row
        leaders.append(row)

    leaders.sort(key=lambda r: -abs(float(r.get("net_flow_score") or 0)))
    for i, row in enumerate(leaders, start=1):
        row["rank"] = i

    bullish = [r["symbol"] for r in leaders if r["sentiment"] == "bullish"]
    bearish = [r["symbol"] for r in leaders if r["sentiment"] == "bearish"]
    alerts_n = len(calls.get("data") or []) + len(puts.get("data") or [])

    return {
        "ok": True,
        "configured": True,
        "source": "unusual_whales",
        "alerts_n": alerts_n,
        "leaders": leaders,
        "bullish_calls": bullish,
        "bearish_puts": bearish,
        "by_symbol": by_symbol,
        "min_premium": min_premium,
        "calls_ok": bool(calls.get("ok")),
        "puts_ok": bool(puts.get("ok")),
    }


def merge_flow_leaders(
    yahoo_leaders: list[dict[str, Any]] | None,
    uw_board: dict[str, Any] | None,
    *,
    prefer_uw: bool = True,
) -> list[dict[str, Any]]:
    """Merge UW leaders over Yahoo proxy leaders (UW wins on symbol clash)."""
    out: dict[str, dict[str, Any]] = {}
    for row in yahoo_leaders or []:
        sym = str(row.get("symbol") or "").upper()
        if sym:
            out[sym] = dict(row)
    if uw_board and uw_board.get("ok"):
        for row in uw_board.get("leaders") or []:
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            if prefer_uw or sym not in out:
                merged = {**(out.get(sym) or {}), **row}
                out[sym] = merged
    leaders = list(out.values())
    leaders.sort(key=lambda r: -abs(float(r.get("net_flow_score") or 0)))
    for i, row in enumerate(leaders, start=1):
        row["rank"] = i
    return leaders


def fetch_market_tide(
    *,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /api/market/market-tide — whole-market call vs put premium tape."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return {"ok": False, "configured": False, "skipped": True, "source": "unusual_whales"}
    try:
        r = requests.get(MARKET_TIDE, headers=_headers(key), timeout=timeout)
        r.raise_for_status()
        payload = r.json() if r.content else {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            return {
                "ok": False,
                "configured": True,
                "source": "unusual_whales",
                "error": "empty market-tide",
            }
        last = data[-1] if isinstance(data[-1], dict) else {}
        call_p = _as_float(last.get("net_call_premium"))
        put_p = _as_float(last.get("net_put_premium"))
        net = call_p - put_p
        if net >= 50_000_000:
            sentiment = "bullish"
        elif net <= -50_000_000:
            sentiment = "bearish"
        else:
            sentiment = "neutral"
        return {
            "ok": True,
            "configured": True,
            "source": "unusual_whales",
            "sentiment": sentiment,
            "net_call_premium": call_p,
            "net_put_premium": put_p,
            "tide_net": net,
            "net_volume": _as_float(last.get("net_volume")),
            "asof": last.get("timestamp") or last.get("date"),
            "points": len(data),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("unusual_whales market-tide failed: %s", exc)
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "error": str(exc),
        }


def fetch_darkpool_recent(
    *,
    api_key: str | None = None,
    limit: int = 40,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /api/darkpool/recent — large ATS prints for desk context."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return {"ok": False, "configured": False, "skipped": True, "source": "unusual_whales"}
    try:
        r = requests.get(
            DARKPOOL_RECENT,
            headers=_headers(key),
            params={"limit": int(limit)},
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json() if r.content else {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            data = []
        by_sym: dict[str, float] = {}
        prints: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = _normalize_ticker(row.get("ticker") or row.get("symbol"))
            if not sym:
                continue
            notional = _as_float(row.get("premium") or row.get("notional") or 0)
            if notional <= 0:
                px = _as_float(row.get("price"))
                sz = _as_float(row.get("size"))
                notional = px * sz
            by_sym[sym] = by_sym.get(sym, 0.0) + notional
            prints.append(
                {
                    "symbol": sym,
                    "price": row.get("price"),
                    "size": row.get("size"),
                    "notional": round(notional, 0),
                    "executed_at": row.get("executed_at") or row.get("timestamp"),
                }
            )
        leaders = sorted(
            [{"symbol": s, "notional": round(v, 0)} for s, v in by_sym.items()],
            key=lambda x: -float(x["notional"]),
        )
        return {
            "ok": True,
            "configured": True,
            "source": "unusual_whales",
            "prints": prints[:limit],
            "leaders": leaders[:20],
            "symbols": [r["symbol"] for r in leaders[:20]],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("unusual_whales darkpool failed: %s", exc)
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "error": str(exc),
            "prints": [],
            "leaders": [],
        }


def build_uw_desk_context(
    *,
    api_key: str | None = None,
    flow_limit: int = 100,
    min_premium: float = 50_000.0,
    timeout: float = 18.0,
) -> dict[str, Any]:
    """Full desk pack: flow leaders + market tide + dark-pool leaders."""
    flow = build_uw_flow_board(
        api_key=api_key,
        limit=flow_limit,
        min_premium=min_premium,
        timeout=timeout,
    )
    tide = fetch_market_tide(api_key=api_key, timeout=min(timeout, 15.0))
    dark = fetch_darkpool_recent(api_key=api_key, limit=40, timeout=min(timeout, 15.0))
    return {
        "ok": bool(flow.get("ok")),
        "configured": bool(flow.get("configured") or tide.get("configured") or dark.get("configured")),
        "source": "unusual_whales",
        "flow": flow,
        "market_tide": tide,
        "darkpool": dark,
        # Flatten common fields so existing challenge/actions code keeps working
        "alerts_n": flow.get("alerts_n"),
        "leaders": flow.get("leaders") or [],
        "bullish_calls": flow.get("bullish_calls") or [],
        "bearish_puts": flow.get("bearish_puts") or [],
        "by_symbol": flow.get("by_symbol") or {},
        "error": flow.get("error") or tide.get("error") or dark.get("error"),
    }
