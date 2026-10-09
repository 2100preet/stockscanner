"""Gex Daddy (https://gex-daddy.onrender.com) — free GEX / walls / flip feed.

Public JSON: GET /api/gex?ticker=SPY
Returns call/put walls, flip level, net GEX bias for index + single-name.

Used to soft-gate Options CALL BUY NOW (into call wall / short-gamma above flip)
and to enrich walls_by_symbol / free dealer cockpit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://gex-daddy.onrender.com"
DEFAULT_TICKERS = ("SPY", "SPX", "QQQ", "IWM")


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def fetch_gex_daddy(
    ticker: str = "SPY",
    *,
    base_url: str = DEFAULT_BASE,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch one ticker profile from Gex Daddy."""
    sym = str(ticker or "SPY").upper().strip()
    url = f"{base_url.rstrip('/')}/api/gex"
    try:
        r = requests.get(url, params={"ticker": sym}, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "ticker": sym, "source": "gex_daddy"}

    if not isinstance(payload, dict) or not payload.get("ok"):
        return {
            "ok": False,
            "error": (payload or {}).get("error") if isinstance(payload, dict) else "bad payload",
            "ticker": sym,
            "source": "gex_daddy",
        }

    data = payload.get("data") or {}
    levels = data.get("levels") or {}
    summary = data.get("summary") or {}
    call_walls = list(levels.get("callWalls") or [])
    put_walls = list(levels.get("putWalls") or [])
    call0 = call_walls[0] if call_walls else {}
    put0 = put_walls[0] if put_walls else {}

    spot = _f(data.get("liveSpot")) or _f(data.get("spot"))
    call_wall = _f(call0.get("strike"))
    put_wall = _f(put0.get("strike"))
    flip = _f(levels.get("flipLevel"))
    net = _f(summary.get("netTotalGEX"))
    bias = str(summary.get("gexBias") or "").lower() or None
    if not bias and net is not None:
        bias = "positive" if net > 0 else ("negative" if net < 0 else "neutral")

    regime = None
    if bias == "positive":
        regime = "LONG_GAMMA"
    elif bias == "negative":
        regime = "SHORT_GAMMA"

    return {
        "ok": True,
        "source": "gex_daddy",
        "ticker": str(data.get("ticker") or sym).upper(),
        "spot": spot,
        "spot_source": data.get("spotSource"),
        "timestamp": data.get("timestamp"),
        "call_wall": call_wall,
        "put_wall": put_wall,
        "call_wall_oi": call0.get("oi"),
        "put_wall_oi": put0.get("oi"),
        "call_walls": [
            {"strike": _f(w.get("strike")), "gex": _f(w.get("gex")), "oi": w.get("oi")}
            for w in call_walls[:5]
        ],
        "put_walls": [
            {"strike": _f(w.get("strike")), "gex": _f(w.get("gex")), "oi": w.get("oi")}
            for w in put_walls[:5]
        ],
        "flip": flip,
        "hvn": levels.get("hvn") or [],
        "net_gex": net,
        "gex_bias": bias,
        "regime": regime,
        "total_call_gex": _f(summary.get("totalCallGEX")),
        "total_put_gex": _f(summary.get("totalPutGEX")),
        "strike_count": summary.get("strikeCount"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            f"Gex Daddy {sym}: bias={bias or '—'} · call wall {call_wall} · "
            f"put wall {put_wall} · flip {flip}"
        ),
    }


def fetch_gex_daddy_board(
    tickers: list[str] | tuple[str, ...] | None = None,
    *,
    base_url: str = DEFAULT_BASE,
    timeout: float = 18.0,
) -> dict[str, Any]:
    """Fetch several tickers; return by_symbol map + walls-ready rows."""
    syms = [str(s).upper() for s in (tickers or DEFAULT_TICKERS) if s]
    by_symbol: dict[str, dict[str, Any]] = {}
    walls: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for sym in syms:
        row = fetch_gex_daddy(sym, base_url=base_url, timeout=timeout)
        if not row.get("ok"):
            errors.append(f"{sym}: {row.get('error')}")
            continue
        by_symbol[row["ticker"]] = row
        walls[row["ticker"]] = {
            "call_wall": row.get("call_wall"),
            "put_wall": row.get("put_wall"),
            "call_wall_oi": row.get("call_wall_oi"),
            "put_wall_oi": row.get("put_wall_oi"),
            "flip": row.get("flip"),
            "regime": row.get("regime"),
            "gex_bias": row.get("gex_bias"),
            "net_gex": row.get("net_gex"),
            "spot": row.get("spot"),
            "source": "gex_daddy",
            "call_walls": row.get("call_walls"),
            "put_walls": row.get("put_walls"),
            "note": row.get("note"),
        }

    summary = []
    for sym, row in by_symbol.items():
        summary.append(row.get("note") or f"{sym} GEX ok")

    return {
        "ok": bool(by_symbol),
        "source": "gex_daddy",
        "base_url": base_url.rstrip("/"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_symbol": by_symbol,
        "walls": walls,
        "summary": summary,
        "errors": errors,
        "note": (
            "Gex Daddy free GEX/walls/flip — used to soft-gate CALL BUY NOW near call walls "
            "and annotate dealer regime. Not VS3D MM books."
        ),
    }


def call_into_wall(
    *,
    spot: float | None,
    call_wall: float | None,
    buffer_pct: float = 0.15,
) -> bool:
    """True when spot is at/above call wall or within buffer_pct below it."""
    if spot is None or call_wall is None or call_wall <= 0 or spot <= 0:
        return False
    thresh = float(call_wall) * (1.0 - max(0.0, buffer_pct) / 100.0)
    return float(spot) >= thresh


def short_gamma_above_flip(
    *,
    spot: float | None,
    flip: float | None,
    gex_bias: str | None,
) -> bool:
    bias = str(gex_bias or "").lower()
    if spot is None or flip is None or flip <= 0:
        return False
    return bias in {"negative", "short", "short_gamma"} and float(spot) > float(flip)
