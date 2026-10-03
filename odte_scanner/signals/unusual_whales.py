"""Unusual Whales options-flow client (Bearer API key).

Env: ``UNUSUAL_WHALES_API_KEY`` — never commit the key; set as a GitHub
Actions secret and pass it into the Pages workflow.

Endpoints used:
  - ``/api/option-trades/flow-alerts`` — call/put flow leaders
  - ``/api/market/market-tide`` — whole-market tide
  - ``/api/darkpool/recent`` — ATS prints
  - ``/api/stock/{ticker}/greek-exposure/expiry`` — GEX by expiry
  - ``/api/stock/{ticker}/flow-per-expiry`` — premium by expiry
  - ``/api/option-contract/{id}/intraday`` — 1m contract ticks

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
GREEK_EXPOSURE_EXPIRY_TMPL = f"{BASE_URL}/api/stock/{{ticker}}/greek-exposure/expiry"
FLOW_PER_EXPIRY_TMPL = f"{BASE_URL}/api/stock/{{ticker}}/flow-per-expiry"
OPTION_CONTRACT_INTRADAY_TMPL = f"{BASE_URL}/api/option-contract/{{id}}/intraday"
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


def _not_configured() -> dict[str, Any]:
    return {
        "ok": False,
        "configured": False,
        "skipped": True,
        "source": "unusual_whales",
        "data": [],
        "error": "UNUSUAL_WHALES_API_KEY not set",
    }


def fetch_greek_exposure_by_expiry(
    ticker: str,
    *,
    api_key: str | None = None,
    date: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /api/stock/{ticker}/greek-exposure/expiry — GEX/delta/vanna by expiry."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return _not_configured()
    sym = _normalize_ticker(ticker)
    if not sym:
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "data": [],
            "error": "ticker required",
        }
    params: dict[str, Any] = {}
    if date:
        params["date"] = str(date)[:10]
    try:
        r = requests.get(
            GREEK_EXPOSURE_EXPIRY_TMPL.format(ticker=sym),
            headers=_headers(key),
            params=params or None,
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "source": "unusual_whales",
                "ticker": sym,
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
            "ticker": sym,
            "data": data,
            "status_code": r.status_code,
            "summary": summarize_greek_by_expiry(data, ticker=sym),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("unusual_whales greek-exposure/expiry %s failed: %s", sym, exc)
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "ticker": sym,
            "data": [],
            "error": str(exc),
        }


def fetch_flow_per_expiry(
    ticker: str,
    *,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /api/stock/{ticker}/flow-per-expiry — call/put premium by expiry."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return _not_configured()
    sym = _normalize_ticker(ticker)
    if not sym:
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "data": [],
            "error": "ticker required",
        }
    try:
        r = requests.get(
            FLOW_PER_EXPIRY_TMPL.format(ticker=sym),
            headers=_headers(key),
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "source": "unusual_whales",
                "ticker": sym,
                "data": [],
                "error": "unauthorized (check API key)",
                "status_code": 401,
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            data = []
        asof = payload.get("date") if isinstance(payload, dict) else None
        return {
            "ok": True,
            "configured": True,
            "source": "unusual_whales",
            "ticker": sym,
            "data": data,
            "asof": asof,
            "status_code": r.status_code,
            "summary": summarize_flow_per_expiry(data, ticker=sym),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("unusual_whales flow-per-expiry %s failed: %s", sym, exc)
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "ticker": sym,
            "data": [],
            "error": str(exc),
        }


def fetch_option_contract_intraday(
    contract_id: str,
    *,
    api_key: str | None = None,
    date: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /api/option-contract/{id}/intraday — 1m bid/ask/mid volume ticks."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return _not_configured()
    cid = str(contract_id or "").upper().strip()
    if not cid:
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "data": [],
            "error": "contract id required",
        }
    params: dict[str, Any] = {}
    if date:
        params["date"] = str(date)[:10]
    try:
        r = requests.get(
            OPTION_CONTRACT_INTRADAY_TMPL.format(id=cid),
            headers=_headers(key),
            params=params or None,
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "source": "unusual_whales",
                "contract": cid,
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
            "contract": cid,
            "data": data,
            "status_code": r.status_code,
            "summary": summarize_contract_intraday(data, contract=cid),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("unusual_whales option-contract intraday %s failed: %s", cid, exc)
        return {
            "ok": False,
            "configured": True,
            "source": "unusual_whales",
            "contract": cid,
            "data": [],
            "error": str(exc),
        }


def summarize_greek_by_expiry(
    rows: list[Any] | None,
    *,
    ticker: str | None = None,
) -> dict[str, Any]:
    """Compact desk summary from greek-exposure/expiry rows."""
    items: list[dict[str, Any]] = []
    net_gex = 0.0
    net_delta = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        call_gex = _as_float(row.get("call_gex"))
        put_gex = _as_float(row.get("put_gex"))
        call_delta = _as_float(row.get("call_delta"))
        put_delta = _as_float(row.get("put_delta"))
        gex = call_gex + put_gex
        delta = call_delta + put_delta
        net_gex += gex
        net_delta += delta
        exp = str(row.get("expiry") or "")[:10]
        items.append(
            {
                "expiry": exp,
                "dte": row.get("dte"),
                "call_gex": round(call_gex, 0),
                "put_gex": round(put_gex, 0),
                "net_gex": round(gex, 0),
                "net_delta": round(delta, 0),
            }
        )
    items.sort(key=lambda r: abs(float(r.get("net_gex") or 0)), reverse=True)
    top = items[:6]
    bias = "call_gex" if net_gex > 0 else ("put_gex" if net_gex < 0 else "neutral")
    return {
        "ticker": _normalize_ticker(ticker) if ticker else None,
        "expiries_n": len(items),
        "net_gex": round(net_gex, 0),
        "net_delta": round(net_delta, 0),
        "bias": bias,
        "top_expiries": top,
        "headline": (
            f"{_normalize_ticker(ticker) or '—'} GEX by expiry "
            f"net {net_gex/1e6:+.1f}M · {len(items)} expiries"
            if items
            else f"{_normalize_ticker(ticker) or '—'} GEX by expiry — empty"
        ),
    }


def summarize_flow_per_expiry(
    rows: list[Any] | None,
    *,
    ticker: str | None = None,
) -> dict[str, Any]:
    """Compact desk summary from flow-per-expiry rows."""
    items: list[dict[str, Any]] = []
    call_prem = 0.0
    put_prem = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        c = _as_float(row.get("call_premium"))
        p = _as_float(row.get("put_premium"))
        call_prem += c
        put_prem += p
        exp = str(row.get("expiry") or "")[:10]
        items.append(
            {
                "expiry": exp,
                "call_premium": round(c, 0),
                "put_premium": round(p, 0),
                "net_premium": round(c - p, 0),
                "call_volume": int(_as_float(row.get("call_volume"))),
                "put_volume": int(_as_float(row.get("put_volume"))),
            }
        )
    items.sort(key=lambda r: abs(float(r.get("net_premium") or 0)), reverse=True)
    net = call_prem - put_prem
    if call_prem > put_prem * 1.15:
        sentiment = "bullish"
    elif put_prem > call_prem * 1.15:
        sentiment = "bearish"
    else:
        sentiment = "neutral"
    return {
        "ticker": _normalize_ticker(ticker) if ticker else None,
        "expiries_n": len(items),
        "call_premium": round(call_prem, 0),
        "put_premium": round(put_prem, 0),
        "net_premium": round(net, 0),
        "sentiment": sentiment,
        "top_expiries": items[:6],
        "headline": (
            f"{_normalize_ticker(ticker) or '—'} flow/expiry "
            f"{sentiment} net ${net/1000:,.0f}k · {len(items)} expiries"
            if items
            else f"{_normalize_ticker(ticker) or '—'} flow/expiry — empty"
        ),
    }


def summarize_contract_intraday(
    rows: list[Any] | None,
    *,
    contract: str | None = None,
) -> dict[str, Any]:
    """Compact desk summary from option-contract intraday minute ticks."""
    ask_vol = bid_vol = mid_vol = 0.0
    ask_prem = bid_prem = 0.0
    last_close = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ask_vol += _as_float(row.get("volume_ask_side"))
        bid_vol += _as_float(row.get("volume_bid_side"))
        mid_vol += _as_float(row.get("volume_mid_side"))
        ask_prem += _as_float(row.get("premium_ask_side"))
        bid_prem += _as_float(row.get("premium_bid_side"))
        if row.get("close") is not None:
            last_close = _as_float(row.get("close"))
    total = ask_vol + bid_vol + mid_vol
    if ask_vol > bid_vol * 1.15:
        side = "ask"
    elif bid_vol > ask_vol * 1.15:
        side = "bid"
    else:
        side = "mixed"
    return {
        "contract": str(contract or "").upper() or None,
        "bars_n": len([r for r in (rows or []) if isinstance(r, dict)]),
        "volume_ask": int(ask_vol),
        "volume_bid": int(bid_vol),
        "volume_mid": int(mid_vol),
        "premium_ask": round(ask_prem, 0),
        "premium_bid": round(bid_prem, 0),
        "last_close": last_close,
        "dominant_side": side,
        "headline": (
            f"{str(contract or '').upper() or '—'} intraday "
            f"{side}-led vol ask {int(ask_vol)}/bid {int(bid_vol)}"
            + (f" · last ${last_close:.2f}" if last_close is not None else "")
            if total > 0
            else f"{str(contract or '').upper() or '—'} intraday — empty"
        ),
    }


def build_uw_expiry_pack(
    ticker: str,
    *,
    api_key: str | None = None,
    date: str | None = None,
    timeout: float = 15.0,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Focused helper: greek-by-expiry + flow-per-expiry for one ticker."""
    greeks = fetch_greek_exposure_by_expiry(
        ticker, api_key=api_key, date=date, timeout=timeout
    )
    flow = fetch_flow_per_expiry(ticker, api_key=api_key, timeout=timeout)
    sym = _normalize_ticker(ticker)
    out: dict[str, Any] = {
        "ok": bool(greeks.get("ok") or flow.get("ok")),
        "configured": bool(greeks.get("configured") or flow.get("configured")),
        "source": "unusual_whales",
        "ticker": sym,
        "greek_by_expiry": greeks.get("summary") or {},
        "flow_per_expiry": flow.get("summary") or {},
        "greeks_ok": bool(greeks.get("ok")),
        "flow_ok": bool(flow.get("ok")),
        "error": greeks.get("error") or flow.get("error"),
    }
    if include_raw:
        out["greek_by_expiry_raw"] = greeks.get("data") or []
        out["flow_per_expiry_raw"] = flow.get("data") or []
    return out


def build_uw_desk_context(
    *,
    api_key: str | None = None,
    flow_limit: int = 100,
    min_premium: float = 50_000.0,
    timeout: float = 18.0,
    focus_tickers: list[str] | None = None,
    focus_contracts: list[str] | None = None,
    max_focus_tickers: int = 4,
    max_focus_contracts: int = 2,
) -> dict[str, Any]:
    """Full desk pack: flow leaders + market tide + dark-pool + optional expiry/intraday."""
    flow = build_uw_flow_board(
        api_key=api_key,
        limit=flow_limit,
        min_premium=min_premium,
        timeout=timeout,
    )
    tide = fetch_market_tide(api_key=api_key, timeout=min(timeout, 15.0))
    dark = fetch_darkpool_recent(api_key=api_key, limit=40, timeout=min(timeout, 15.0))

    # Optional per-ticker expiry pack + contract intraday (capped — Pages budget)
    by_ticker: dict[str, dict[str, Any]] = {}
    tickers: list[str] = []
    for raw in focus_tickers or []:
        sym = _normalize_ticker(raw)
        if sym and sym not in tickers:
            tickers.append(sym)
        if len(tickers) >= max(0, int(max_focus_tickers)):
            break
    for sym in tickers:
        pack = build_uw_expiry_pack(
            sym, api_key=api_key, timeout=min(timeout, 12.0), include_raw=False
        )
        by_ticker[sym] = pack

    contracts_out: dict[str, dict[str, Any]] = {}
    cids: list[str] = []
    for raw in focus_contracts or []:
        cid = str(raw or "").upper().strip()
        if cid and cid not in cids:
            cids.append(cid)
        if len(cids) >= max(0, int(max_focus_contracts)):
            break
    for cid in cids:
        intra = fetch_option_contract_intraday(
            cid, api_key=api_key, timeout=min(timeout, 12.0)
        )
        contracts_out[cid] = {
            "ok": bool(intra.get("ok")),
            "contract": cid,
            "summary": intra.get("summary") or {},
            "error": intra.get("error"),
            "bars_n": len(intra.get("data") or []),
        }

    expiry_headlines = [
        (by_ticker[s].get("flow_per_expiry") or {}).get("headline")
        or (by_ticker[s].get("greek_by_expiry") or {}).get("headline")
        for s in tickers
        if by_ticker.get(s, {}).get("ok")
    ]
    expiry_headlines = [h for h in expiry_headlines if h]

    return {
        "ok": bool(flow.get("ok")),
        "configured": bool(flow.get("configured") or tide.get("configured") or dark.get("configured")),
        "source": "unusual_whales",
        "flow": flow,
        "market_tide": tide,
        "darkpool": dark,
        "greek_flow_by_ticker": by_ticker,
        "contract_intraday": contracts_out,
        "expiry_headlines": expiry_headlines[:8],
        # Flatten common fields so existing challenge/actions code keeps working
        "alerts_n": flow.get("alerts_n"),
        "leaders": flow.get("leaders") or [],
        "bullish_calls": flow.get("bullish_calls") or [],
        "bearish_puts": flow.get("bearish_puts") or [],
        "by_symbol": flow.get("by_symbol") or {},
        "error": flow.get("error") or tide.get("error") or dark.get("error"),
    }
