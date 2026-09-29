"""Tradier brokerage market-data client.

Env:
  TRADIER_ACCESS_TOKEN  — required (production preferred for live marks)
  TRADIER_ACCOUNT_ID    — optional (orders later)
  TRADIER_SANDBOX       — ``1``/``true`` → sandbox (15m delayed); default production

Endpoints used (https://docs.tradier.com/):
  GET/POST /markets/quotes
  GET /markets/options/chains
  GET /markets/options/expirations
  GET /markets/options/strikes
  GET /markets/history
  GET /markets/timesales
  GET /markets/clock
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logger = logging.getLogger(__name__)

PROD_BASE = "https://api.tradier.com/v1"
SANDBOX_BASE = "https://sandbox.tradier.com/v1"
ET = ZoneInfo("America/New_York")


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


def configured() -> bool:
    return bool(access_token_from_env())


def status() -> dict[str, Any]:
    tok = access_token_from_env()
    return {
        "configured": bool(tok),
        "sandbox": use_sandbox(),
        "base": _base_url(),
        "account_id_set": bool((os.environ.get("TRADIER_ACCOUNT_ID") or "").strip()),
        "token_len": len(tok) if tok else 0,
        "source": "tradier",
        "endpoints": [
            "quotes",
            "options/chains",
            "options/expirations",
            "options/strikes",
            "history",
            "timesales",
            "clock",
        ],
    }


def _normalize_quote_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    qblock = (payload.get("quotes") or {}) if isinstance(payload, dict) else {}
    raw = qblock.get("quote")
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _quote_row(row: dict[str, Any]) -> dict[str, Any]:
    sym = str(row.get("symbol") or "").upper()
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else None
    out: dict[str, Any] = {
        "symbol": sym,
        "bid": _as_float(row.get("bid")),
        "ask": _as_float(row.get("ask")),
        "last": _as_float(row.get("last")),
        "open": _as_float(row.get("open")),
        "high": _as_float(row.get("high")),
        "low": _as_float(row.get("low")),
        "close": _as_float(row.get("close")),
        "prevclose": _as_float(row.get("prevclose")),
        "change": _as_float(row.get("change")),
        "change_percentage": _as_float(row.get("change_percentage")),
        "volume": int(_as_float(row.get("volume"))),
        "open_interest": int(_as_float(row.get("open_interest"))),
        "underlying": row.get("underlying"),
        "description": row.get("description"),
        "type": row.get("type"),
        "exch": row.get("exch"),
        "trade_date": row.get("trade_date"),
        "source": "tradier",
    }
    if greeks:
        out["greeks"] = {
            "delta": _as_float(greeks.get("delta")),
            "gamma": _as_float(greeks.get("gamma")),
            "theta": _as_float(greeks.get("theta")),
            "vega": _as_float(greeks.get("vega")),
            "rho": _as_float(greeks.get("rho")),
            "mid_iv": _as_float(greeks.get("mid_iv")),
            "bid_iv": _as_float(greeks.get("bid_iv")),
            "ask_iv": _as_float(greeks.get("ask_iv")),
        }
    return out


def fetch_quotes(
    symbols: list[str],
    *,
    token: str | None = None,
    greeks: bool = False,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """GET/POST /markets/quotes for equity or OCC option symbols."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "quotes": {},
        }
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    # de-dupe preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for s in syms:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    if not ordered:
        return {"ok": True, "configured": True, "quotes": {}, "n": 0}

    out: dict[str, dict[str, Any]] = {}
    # Tradier GET caps around ~100 symbols; chunk + use POST for large lists
    chunk_size = 80
    try:
        for i in range(0, len(ordered), chunk_size):
            chunk = ordered[i : i + chunk_size]
            params = {
                "symbols": ",".join(chunk),
                "greeks": "true" if greeks else "false",
            }
            if len(chunk) > 25:
                r = requests.post(
                    f"{_base_url()}/markets/quotes",
                    headers={**_headers(key), "Content-Type": "application/x-www-form-urlencoded"},
                    data=params,
                    timeout=timeout,
                )
            else:
                r = requests.get(
                    f"{_base_url()}/markets/quotes",
                    headers=_headers(key),
                    params=params,
                    timeout=timeout,
                )
            if r.status_code == 401:
                return {
                    "ok": False,
                    "configured": True,
                    "error": "unauthorized (check TRADIER_ACCESS_TOKEN)",
                    "status_code": 401,
                    "quotes": out,
                }
            r.raise_for_status()
            payload = r.json() if r.content else {}
            for row in _normalize_quote_rows(payload):
                parsed = _quote_row(row)
                if parsed["symbol"]:
                    out[parsed["symbol"]] = parsed
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
            "quotes": out,
            "n": len(out),
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


def fetch_expirations(
    symbol: str,
    *,
    token: str | None = None,
    include_all_roots: bool = True,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """GET /markets/options/expirations."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "dates": [],
        }
    try:
        r = requests.get(
            f"{_base_url()}/markets/options/expirations",
            headers=_headers(key),
            params={
                "symbol": str(symbol).upper(),
                "includeAllRoots": "true" if include_all_roots else "false",
            },
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "error": "unauthorized",
                "status_code": 401,
                "dates": [],
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        block = (payload.get("expirations") or {}) if isinstance(payload, dict) else {}
        raw = block.get("date")
        if isinstance(raw, list):
            dates = [str(x) for x in raw if x]
        elif raw:
            dates = [str(raw)]
        else:
            dates = []
        return {
            "ok": True,
            "configured": True,
            "sandbox": use_sandbox(),
            "symbol": str(symbol).upper(),
            "dates": dates,
            "n": len(dates),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier expirations failed %s: %s", symbol, exc)
        return {
            "ok": False,
            "configured": True,
            "error": str(exc),
            "dates": [],
        }


def fetch_strikes(
    symbol: str,
    expiry: str,
    *,
    token: str | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """GET /markets/options/strikes."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "strikes": [],
        }
    try:
        r = requests.get(
            f"{_base_url()}/markets/options/strikes",
            headers=_headers(key),
            params={"symbol": str(symbol).upper(), "expiration": str(expiry)},
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json() if r.content else {}
        block = (payload.get("strikes") or {}) if isinstance(payload, dict) else {}
        raw = block.get("strike")
        if isinstance(raw, list):
            strikes = [float(x) for x in raw]
        elif raw is not None:
            strikes = [float(raw)]
        else:
            strikes = []
        return {
            "ok": True,
            "configured": True,
            "symbol": str(symbol).upper(),
            "expiry": str(expiry),
            "strikes": strikes,
            "n": len(strikes),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier strikes failed %s %s: %s", symbol, expiry, exc)
        return {"ok": False, "configured": True, "error": str(exc), "strikes": []}


def fetch_clock(*, token: str | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """GET /markets/clock — session state for confidence / offline gating."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
        }
    try:
        r = requests.get(
            f"{_base_url()}/markets/clock",
            headers=_headers(key),
            timeout=timeout,
        )
        if r.status_code == 401:
            return {
                "ok": False,
                "configured": True,
                "error": "unauthorized",
                "status_code": 401,
            }
        r.raise_for_status()
        payload = r.json() if r.content else {}
        clock = (payload.get("clock") or payload) if isinstance(payload, dict) else {}
        state = str(clock.get("state") or "").lower()
        return {
            "ok": True,
            "configured": True,
            "sandbox": use_sandbox(),
            "state": state,
            "description": clock.get("description"),
            "next_change": clock.get("next_change"),
            "next_state": clock.get("next_state"),
            "timestamp": clock.get("timestamp"),
            "date": clock.get("date"),
            "source": "tradier",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier clock failed: %s", exc)
        return {"ok": False, "configured": True, "error": str(exc)}


def fetch_history(
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
    interval: str = "daily",
    token: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GET /markets/history — daily/weekly/monthly bars."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "bars": [],
        }
    params: dict[str, str] = {
        "symbol": str(symbol).upper(),
        "interval": str(interval),
    }
    if start:
        params["start"] = str(start)
    if end:
        params["end"] = str(end)
    try:
        r = requests.get(
            f"{_base_url()}/markets/history",
            headers=_headers(key),
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json() if r.content else {}
        block = (payload.get("history") or {}) if isinstance(payload, dict) else {}
        raw = block.get("day")
        if isinstance(raw, list):
            rows = [x for x in raw if isinstance(x, dict)]
        elif isinstance(raw, dict):
            rows = [raw]
        else:
            rows = []
        return {
            "ok": True,
            "configured": True,
            "symbol": str(symbol).upper(),
            "bars": rows,
            "n": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier history failed %s: %s", symbol, exc)
        return {"ok": False, "configured": True, "error": str(exc), "bars": []}


def fetch_timesales(
    symbol: str,
    *,
    interval: str = "1min",
    start: str | None = None,
    end: str | None = None,
    session_filter: str = "open",
    token: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """GET /markets/timesales — intraday bars (1min/5min/15min)."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "TRADIER_ACCESS_TOKEN not set",
            "bars": [],
        }
    # Default to today RTH window in ET
    now_et = datetime.now(ET)
    day = now_et.date()
    if not start:
        start = f"{day.isoformat()} 09:30"
    if not end:
        end = f"{day.isoformat()} {now_et.strftime('%H:%M')}"
    params = {
        "symbol": str(symbol).upper(),
        "interval": interval,
        "start": start,
        "end": end,
        "session_filter": session_filter,
    }
    try:
        r = requests.get(
            f"{_base_url()}/markets/timesales",
            headers=_headers(key),
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json() if r.content else {}
        block = (payload.get("series") or {}) if isinstance(payload, dict) else {}
        raw = block.get("data")
        if isinstance(raw, list):
            rows = [x for x in raw if isinstance(x, dict)]
        elif isinstance(raw, dict):
            rows = [raw]
        else:
            rows = []
        return {
            "ok": True,
            "configured": True,
            "sandbox": use_sandbox(),
            "symbol": str(symbol).upper(),
            "bars": rows,
            "n": len(rows),
            "interval": interval,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tradier timesales failed %s: %s", symbol, exc)
        return {"ok": False, "configured": True, "error": str(exc), "bars": []}


def timesales_to_dataframe(result: dict[str, Any]) -> pd.DataFrame | None:
    """Convert Tradier timesales payload to OHLCV DataFrame (ET index)."""
    rows = result.get("bars") if isinstance(result, dict) else None
    if not rows:
        return None
    records: list[dict[str, Any]] = []
    idx: list[pd.Timestamp] = []
    for row in rows:
        ts_raw = row.get("time") or row.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize(ET)
            else:
                ts = ts.tz_convert(ET)
        except Exception:  # noqa: BLE001
            continue
        idx.append(ts)
        records.append(
            {
                "Open": _as_float(row.get("open") or row.get("price")),
                "High": _as_float(row.get("high") or row.get("price")),
                "Low": _as_float(row.get("low") or row.get("price")),
                "Close": _as_float(row.get("close") or row.get("price")),
                "Volume": _as_float(row.get("volume")),
            }
        )
    if not records:
        return None
    return pd.DataFrame(records, index=pd.DatetimeIndex(idx))


def fetch_intraday_bars(
    symbol: str,
    *,
    interval: str = "1min",
    lookback_days: int = 1,
    token: str | None = None,
) -> pd.DataFrame | None:
    """Convenience: Tradier timesales → OHLCV frame for ORB / power-hour."""
    if not (token or access_token_from_env()):
        return None
    now_et = datetime.now(ET)
    start_day = (now_et.date() - timedelta(days=max(0, lookback_days - 1))).isoformat()
    end = f"{now_et.date().isoformat()} {now_et.strftime('%H:%M')}"
    start = f"{start_day} 09:30"
    res = fetch_timesales(
        symbol,
        interval=interval,
        start=start,
        end=end,
        session_filter="open",
        token=token,
    )
    if not res.get("ok"):
        # Retry 5min if 1min empty
        if interval == "1min":
            res5 = fetch_timesales(
                symbol,
                interval="5min",
                start=start,
                end=end,
                session_filter="open",
                token=token,
            )
            return timesales_to_dataframe(res5)
        return None
    df = timesales_to_dataframe(res)
    if df is not None and not df.empty:
        return df
    if interval == "1min":
        res5 = fetch_timesales(
            symbol,
            interval="5min",
            start=start,
            end=end,
            session_filter="open",
            token=token,
        )
        return timesales_to_dataframe(res5)
    return None


def quote_to_live_dict(row: dict[str, Any], *, symbol: str | None = None) -> dict[str, Any]:
    """Map a Tradier equity quote into the desk LiveQuote dict shape."""
    sym = str(symbol or row.get("symbol") or "").upper()
    last = _as_float(row.get("last")) or _as_float(row.get("close"))
    prev = _as_float(row.get("prevclose")) or (last - _as_float(row.get("change")))
    if last <= 0 and prev <= 0:
        return {}
    if last <= 0:
        last = prev
    if prev <= 0:
        prev = last
    chg = last - prev
    sess_open = _as_float(row.get("open")) or None
    day_high = _as_float(row.get("high")) or None
    day_low = _as_float(row.get("low")) or None
    dist_high = None
    if day_high and day_high > 0 and last:
        dist_high = (last / day_high - 1.0) * 100.0
    asof = datetime.now(timezone_utc()).isoformat()
    return {
        "symbol": sym,
        "last": last,
        "prev_close": prev,
        "change": chg,
        "change_pct": (chg / prev) * 100.0 if prev else 0.0,
        "session": "regular",
        "asof": asof,
        "day_high": day_high or None,
        "day_low": day_low or None,
        "session_open": sess_open,
        "session_change": (last - sess_open) if sess_open else None,
        "session_change_pct": ((last - sess_open) / sess_open * 100.0) if sess_open else None,
        "mom_5m_pct": None,
        "mom_15m_pct": None,
        "dist_from_day_high_pct": dist_high,
        "source": "tradier",
        "mark_source": "tradier",
    }


def timezone_utc():
    from datetime import timezone

    return timezone.utc


def fetch_option_quote(
    *,
    symbol: str,
    expiry: str,
    strike: float,
    right: str = "call",
    contract: str | None = None,
    token: str | None = None,
    timeout: float = 15.0,
    greeks: bool = False,
) -> dict[str, Any] | None:
    """Best-effort Tradier mark for one contract (OCC quote or chain strike match)."""
    key = token if token is not None else access_token_from_env()
    if not key:
        return None

    if contract:
        q = fetch_quotes([str(contract)], token=key, timeout=timeout, greeks=greeks)
        row = (q.get("quotes") or {}).get(str(contract).upper())
        if q.get("ok") and row and (row.get("ask") or row.get("bid") or row.get("last")):
            return {
                **row,
                "expiry": expiry,
                "strike": float(strike),
                "right": "call" if str(right).lower().startswith("c") else "put",
                "underlying": symbol,
            }

    chain = fetch_option_chain(symbol, expiry, token=key, timeout=timeout, greeks=greeks)
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
    out = {
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
    g = best.get("greeks")
    if isinstance(g, dict):
        out["greeks"] = g
    return out


def pick_option_contract(
    symbol: str,
    spot: float,
    *,
    right: str = "C",
    min_dte: int = 0,
    max_dte: int = 21,
    prefer_dte: int = 3,
    otm_pct_max: float = 8.0,
    itm_pct_max: float = 2.0,
    min_volume: int = 25,
    min_oi: int = 200,
    require_bid: bool = True,
    greeks: bool = False,
    token: str | None = None,
) -> dict[str, Any] | None:
    """Select a liquid option via Tradier expirations + chains (Yahoo-free)."""
    if not (token or access_token_from_env()):
        return None
    right_u = str(right).upper()
    want_put = right_u.startswith("P")
    exp = fetch_expirations(symbol, token=token)
    if not exp.get("ok") or not exp.get("dates"):
        return None

    today = date.today()
    targets: list[tuple[str, int]] = []
    for d in exp["dates"]:
        try:
            ed = date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        dte = (ed - today).days
        if min_dte <= dte <= max_dte:
            targets.append((str(d)[:10], dte))
    if not targets and max_dte <= 21:
        for d in exp["dates"]:
            try:
                ed = date.fromisoformat(str(d)[:10])
            except ValueError:
                continue
            dte = (ed - today).days
            if 0 <= dte <= max(max_dte, 14):
                targets.append((str(d)[:10], dte))
    if not targets:
        return None
    targets.sort(key=lambda x: abs(x[1] - prefer_dte))

    # Refresh spot from Tradier when possible
    live_spot = float(spot)
    q = fetch_quotes([str(symbol).upper()], token=token)
    row = (q.get("quotes") or {}).get(str(symbol).upper())
    if row and _as_float(row.get("last")) > 0:
        live_spot = _as_float(row.get("last"))
    if live_spot <= 0:
        return None

    best: dict[str, Any] | None = None
    best_rank = -1e18
    for expiry, dte in targets[:5]:
        chain = fetch_option_chain(symbol, expiry, token=token, greeks=greeks)
        if not chain.get("ok"):
            continue
        for opt in chain.get("options") or []:
            opt_type = str(opt.get("option_type") or opt.get("type") or "").lower()
            is_put = opt_type in {"put", "p"}
            if is_put != want_put:
                continue
            strike = _as_float(opt.get("strike"))
            if strike <= 0:
                continue
            mny = (strike - live_spot) / live_spot * 100.0
            if not want_put:
                if mny < -itm_pct_max or mny > otm_pct_max:
                    continue
                otm_target = 3.0
            else:
                if mny > itm_pct_max or mny < -otm_pct_max:
                    continue
                otm_target = -3.0
            bid = _as_float(opt.get("bid"))
            ask = _as_float(opt.get("ask"))
            last = _as_float(opt.get("last"))
            oi = int(_as_float(opt.get("open_interest")))
            vol = int(_as_float(opt.get("volume")))
            if vol < min_volume or oi < min_oi:
                continue
            mark_source = "ask"
            if ask <= 0 and last > 0:
                ask = last
                bid = bid or round(last * 0.95, 2)
                mark_source = "last"
            if ask <= 0:
                continue
            if require_bid and bid <= 0:
                continue
            spread = ((ask - bid) / ask) if ask and bid > 0 else 0.5
            if spread > 0.35 and vol < min_volume:
                continue
            rank = (
                50.0
                - abs(mny - otm_target) * 4.0
                - abs(dte - prefer_dte) * 0.04
                - spread * 30.0
                + min(30.0, oi / 150.0)
                + min(50.0, vol / 15.0)
                + (8.0 if mark_source == "ask" and bid > 0 else 0.0)
            )
            if rank > best_rank:
                best_rank = rank
                best = {
                    "symbol": str(symbol).upper(),
                    "right": "P" if want_put else "C",
                    "contract": str(opt.get("symbol") or ""),
                    "expiry": expiry,
                    "dte": dte,
                    "strike": strike,
                    "spot": round(live_spot, 4),
                    "bid": round(bid, 2) if bid > 0 else None,
                    "ask": round(ask, 2),
                    "last": round(last, 2) if last > 0 else None,
                    "mark_source": "tradier",
                    "moneyness_pct": round(mny, 3),
                    "open_interest": oi,
                    "volume": vol,
                    "liquid": bool(vol >= min_volume and oi >= min_oi and bid > 0),
                    "style": ("leap" if dte >= 180 else ("sprint" if dte <= 10 else "swing")),
                    "live": True,
                    "suggested_zone": False,
                    "source": "tradier",
                }
    return best


def probe(*, token: str | None = None) -> dict[str, Any]:
    """Smoke-test token: clock + SPY quote. Used by Pages export status."""
    st = status()
    if not st.get("configured"):
        return {**st, "ok": False, "error": "TRADIER_ACCESS_TOKEN not set"}
    clock = fetch_clock(token=token, timeout=8.0)
    quotes = fetch_quotes(["SPY"], token=token, timeout=10.0)
    ok = bool(quotes.get("ok")) and bool(clock.get("ok") or quotes.get("n"))
    return {
        **st,
        "ok": ok,
        "clock": {
            "ok": clock.get("ok"),
            "state": clock.get("state"),
            "description": clock.get("description"),
            "error": clock.get("error"),
        },
        "smoke_n": quotes.get("n"),
        "error": quotes.get("error") or (None if ok else clock.get("error")),
        "confidence_boost": "marks+clock+chains+timesales" if ok else None,
    }
