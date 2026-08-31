"""Full call+put strike ladders from Yahoo for Echo flow / GEX."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parents[2] / "outputs" / "echo_ladders"
_DEFAULT_TTL_SEC = 900  # 15m — avoid Yahoo rate limits on UI poll


def _nearest_expiries(expirations: list[str], *, max_dte: int = 7) -> list[tuple[str, int]]:
    today = datetime.now().date()
    out: list[tuple[str, int]] = []
    for exp in expirations:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (d - today).days
        if 0 <= dte <= max_dte:
            out.append((exp, dte))
    out.sort(key=lambda x: x[1])
    return out


def _rows_to_side(table, *, right: str, spot: float) -> list[dict[str, Any]]:
    if table is None or getattr(table, "empty", True):
        return []
    rows: list[dict[str, Any]] = []
    for _, r in table.iterrows():
        try:
            strike = float(r.get("strike") or 0)
            bid = float(r.get("bid") or 0)
            ask = float(r.get("ask") or 0)
            last = float(r.get("lastPrice") or 0)
            if ask <= 0 and last > 0:
                ask = last
            if bid <= 0 and last > 0:
                bid = max(0.01, last * 0.95)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (ask or last or bid)
            vol = int(r.get("volume") or 0)
            oi = int(r.get("openInterest") or 0)
            iv = r.get("impliedVolatility")
            iv_f = float(iv) if iv is not None and float(iv) > 0 else None
            mny = ((strike - spot) / spot * 100.0) if spot > 0 else None
            rows.append(
                {
                    "right": right,
                    "strike": strike,
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "last": round(last, 4),
                    "mid": round(float(mid or 0), 4),
                    "volume": vol,
                    "open_interest": oi,
                    "iv": round(iv_f, 4) if iv_f is not None else None,
                    "moneyness_pct": round(mny, 3) if mny is not None else None,
                    "contract": str(r.get("contractSymbol") or ""),
                    "premium_notional": round(float(mid or 0) * 100 * max(vol, 0), 2),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return rows


def _cache_path(symbol: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{symbol.upper()}.json"


def load_cached_ladder(symbol: str, *, ttl_sec: int = _DEFAULT_TTL_SEC) -> dict[str, Any] | None:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        age = time.time() - float(raw.get("_cached_at") or 0)
        if age > ttl_sec:
            return None
        ladder = raw.get("ladder")
        return ladder if isinstance(ladder, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save_cached_ladder(symbol: str, ladder: dict[str, Any]) -> None:
    try:
        path = _cache_path(symbol)
        path.write_text(json.dumps({"_cached_at": time.time(), "ladder": ladder}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("echo ladder cache write failed %s: %s", symbol, exc)


def fetch_option_ladder(
    symbol: str,
    *,
    yahoo_symbol: str | None = None,
    spot: float | None = None,
    max_dte: int = 7,
    prefer_dte: int | None = None,
    use_cache: bool = True,
    cache_ttl_sec: int = _DEFAULT_TTL_SEC,
) -> dict[str, Any] | None:
    """Nearest short-dated call+put ladder for one symbol."""
    if use_cache:
        cached = load_cached_ladder(symbol, ttl_sec=cache_ttl_sec)
        if cached:
            if spot and spot > 0:
                cached = dict(cached)
                cached["spot"] = float(spot)
            return cached

    prev_ladder = None
    try:
        prev_ladder = load_cached_ladder(symbol, ttl_sec=86400 * 30)
    except Exception:  # noqa: BLE001
        prev_ladder = None
    fetch_sym = yahoo_symbol or symbol
    try:
        t = yf.Ticker(fetch_sym)
        exps = list(t.options or [])
        if not exps:
            # Stale cache better than nothing when rate-limited
            return load_cached_ladder(symbol, ttl_sec=cache_ttl_sec * 8)
        window = _nearest_expiries(exps, max_dte=max_dte)
        if not window:
            return load_cached_ladder(symbol, ttl_sec=cache_ttl_sec * 8)
        if prefer_dte is not None:
            window = sorted(window, key=lambda x: abs(x[1] - prefer_dte))
        expiry, dte = window[0]
        chain = t.option_chain(expiry)
        if spot is None or spot <= 0:
            try:
                spot = float(t.fast_info.last_price)
            except Exception:  # noqa: BLE001
                spot = 0.0
        calls = _rows_to_side(chain.calls, right="C", spot=float(spot or 0))
        puts = _rows_to_side(chain.puts, right="P", spot=float(spot or 0))
        if not calls and not puts:
            return load_cached_ladder(symbol, ttl_sec=cache_ttl_sec * 8)
        ladder = {
            "symbol": symbol,
            "yahoo_symbol": fetch_sym,
            "expiry": expiry,
            "dte": dte,
            "spot": float(spot or 0),
            "calls": calls,
            "puts": puts,
        }
        from odte_scanner.echo.flow_deltas import attach_deltas_to_ladder

        ladder = attach_deltas_to_ladder(ladder, prev=prev_ladder)
        save_cached_ladder(symbol, ladder)
        return ladder
    except Exception as exc:  # noqa: BLE001
        logger.debug("echo ladder failed %s: %s", symbol, exc)
        # Rate limits / transient Yahoo errors → serve stale cache if present
        return load_cached_ladder(symbol, ttl_sec=cache_ttl_sec * 8)
