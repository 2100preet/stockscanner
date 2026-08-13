"""DealerEdge-style GEX approximation from Yahoo call/put open interest.

Uses a simple Black-Scholes gamma × OI proxy. This is NOT SpotGamma / dealer tape —
research only.
"""
from __future__ import annotations

import math
from typing import Any


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_gamma(spot: float, strike: float, t_years: float, iv: float, r: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return 0.0
    denom = iv * math.sqrt(t_years)
    if denom <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / denom
    return _norm_pdf(d1) / (spot * denom)


def _pick_iv(row: dict[str, Any], default: float = 0.28) -> float:
    iv = row.get("iv")
    if iv is None:
        return default
    try:
        v = float(iv)
    except (TypeError, ValueError):
        return default
    # Yahoo sometimes returns 0–1, sometimes percent-like
    if v > 3:
        v = v / 100.0
    return max(0.05, min(2.5, v))


def compute_gex_profile(ladder: dict[str, Any]) -> dict[str, Any]:
    spot = float(ladder.get("spot") or 0)
    dte = int(ladder.get("dte") if ladder.get("dte") is not None else 0)
    t_years = max(1.0 / 365.0, (dte + 0.35) / 365.0)
    calls = {float(r["strike"]): r for r in (ladder.get("calls") or [])}
    puts = {float(r["strike"]): r for r in (ladder.get("puts") or [])}
    strikes = sorted(set(calls) | set(puts))

    by_strike: list[dict[str, Any]] = []
    net = 0.0
    call_wall = None
    put_wall = None
    call_wall_oi = -1
    put_wall_oi = -1

    for k in strikes:
        c = calls.get(k) or {}
        p = puts.get(k) or {}
        c_oi = int(c.get("open_interest") or 0)
        p_oi = int(p.get("open_interest") or 0)
        c_iv = _pick_iv(c)
        p_iv = _pick_iv(p)
        c_g = _bs_gamma(spot, k, t_years, c_iv) if c_oi else 0.0
        p_g = _bs_gamma(spot, k, t_years, p_iv) if p_oi else 0.0
        # Dealer short options assumption: call GEX positive, put GEX negative
        call_gex = c_oi * 100 * c_g * spot * spot * 0.01
        put_gex = -p_oi * 100 * p_g * spot * spot * 0.01
        gex = call_gex + put_gex
        net += gex
        by_strike.append(
            {
                "strike": k,
                "call_oi": c_oi,
                "put_oi": p_oi,
                "call_gex": round(call_gex, 2),
                "put_gex": round(put_gex, 2),
                "gex": round(gex, 2),
            }
        )
        if spot and k >= spot and c_oi > call_wall_oi:
            call_wall_oi = c_oi
            call_wall = k
        if spot and k <= spot and p_oi > put_wall_oi:
            put_wall_oi = p_oi
            put_wall = k

    # Gamma flip ≈ strike nearest where cumulative GEX crosses zero walking up from puts
    flip = None
    cum = 0.0
    prev_k = None
    for row in by_strike:
        cum += float(row["gex"])
        if prev_k is not None and cum == 0:
            flip = row["strike"]
            break
        if prev_k is not None and (cum > 0) != (cum - float(row["gex"]) > 0):
            flip = row["strike"]
            break
        prev_k = row["strike"]
    if flip is None and by_strike:
        # fallback: max |gex| below/above blend near spot
        flip = min(by_strike, key=lambda r: abs(float(r["strike"]) - spot))["strike"]

    # HVL proxy: volume-weighted strike across calls+puts
    hvl = None
    num = 0.0
    den = 0.0
    for side in ("calls", "puts"):
        for r in ladder.get(side) or []:
            v = int(r.get("volume") or 0)
            if v <= 0:
                continue
            num += float(r["strike"]) * v
            den += v
    if den > 0:
        hvl = round(num / den, 2)

    # Keep nearby strikes for heatmap UI
    near = [r for r in by_strike if spot <= 0 or abs(r["strike"] - spot) / spot <= 0.08]
    near.sort(key=lambda r: abs(r["strike"] - spot))
    near = sorted(near[:24], key=lambda r: r["strike"])

    regime = "positive_gamma" if net >= 0 else "negative_gamma"
    return {
        "symbol": ladder.get("symbol"),
        "expiry": ladder.get("expiry"),
        "dte": dte,
        "spot": spot,
        "method": "bs_gamma_oi_yahoo",
        "net_gex": round(net, 2),
        "regime": regime,
        "flip": flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "hvl": hvl,
        "by_strike": near,
        "note": "Approx dealer GEX from Yahoo OI + BS gamma — not live dealer positioning.",
    }


def build_dealer_edge(ladders: list[dict[str, Any]], *, max_profiles: int = 10) -> dict[str, Any]:
    profiles = [compute_gex_profile(l) for l in ladders]
    profiles = [p for p in profiles if p.get("by_strike")]
    profiles.sort(key=lambda p: abs(float(p.get("net_gex") or 0)), reverse=True)
    profiles = profiles[:max_profiles]
    return {
        "profiles": profiles,
        "primary": profiles[0] if profiles else None,
        "note": (
            "DealerEdge proxy: call/put walls = max OI strikes; flip/HVL approximated. "
            "Not affiliated with Trade Echo / SpotGamma."
        ),
    }
