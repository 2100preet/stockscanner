"""OptionFlow-style unusual activity from Yahoo chain snapshots.

Tiers mirror Trade Echo naming (Aggressive / Unusual / Golden) but are
computed from volume, OI, and premium notional — not OPRA time & sales.
"""
from __future__ import annotations

from typing import Any


def _vol_oi_ratio(vol: int, oi: int) -> float | None:
    if oi <= 0:
        return None if vol <= 0 else float(vol)  # treat as extreme when OI missing
    return vol / oi


def score_flow_print(row: dict[str, Any], *, spot: float) -> dict[str, Any]:
    vol = int(row.get("volume") or 0)
    oi = int(row.get("open_interest") or 0)
    mid = float(row.get("mid") or row.get("ask") or 0)
    premium = float(row.get("premium_notional") or (mid * 100 * vol))
    ratio = _vol_oi_ratio(vol, oi)
    right = str(row.get("right") or "C")
    strike = float(row.get("strike") or 0)
    mny = row.get("moneyness_pct")
    if mny is None and spot > 0 and strike:
        mny = (strike - spot) / spot * 100.0

    # Signed flow score: calls + / puts − ; magnitude from premium + unusualness
    mag = 0.0
    mag += min(40.0, premium / 25_000.0)  # $1M notional → ~40
    if ratio is not None:
        mag += min(35.0, ratio * 25.0)
    mag += min(15.0, vol / 2000.0)
    if mid > 0:
        mag += min(10.0, 2.0 / max(mid, 0.2))  # cheaper contracts can be more levered flow
    sign = 1.0 if right == "C" else -1.0
    flow_score = max(-100.0, min(100.0, sign * mag))

    tier = "aggressive"
    flags: list[str] = []
    if premium >= 250_000 or (ratio is not None and ratio >= 0.95) or vol >= 20_000:
        tier = "golden"
        flags.append("golden")
    elif premium >= 100_000 or (ratio is not None and ratio >= 0.5) or vol >= 5_000:
        tier = "unusual"
        flags.append("unusual")
    else:
        flags.append("aggressive")

    if ratio is not None and ratio >= 1.0:
        flags.append("vol_gt_oi")
    if premium >= 100_000:
        flags.append("large_premium")
    if right == "C" and mny is not None and 0 <= float(mny) <= 3:
        flags.append("near_atm_call")
    if right == "P" and mny is not None and -3 <= float(mny) <= 0:
        flags.append("near_atm_put")

    return {
        **row,
        "premium_notional": round(premium, 2),
        "vol_oi_ratio": round(ratio, 3) if ratio is not None else None,
        "flow_score": round(flow_score, 1),
        "tier": tier,
        "flags": flags,
        "sentiment": "bullish" if flow_score > 0 else ("bearish" if flow_score < 0 else "neutral"),
    }


def build_option_flow(
    ladders: list[dict[str, Any]],
    *,
    min_volume: int = 200,
    min_premium: float = 15_000,
    max_rows: int = 40,
) -> dict[str, Any]:
    prints: list[dict[str, Any]] = []
    for ladder in ladders:
        sym = ladder["symbol"]
        spot = float(ladder.get("spot") or 0)
        expiry = ladder.get("expiry")
        dte = ladder.get("dte")
        for side in ("calls", "puts"):
            for row in ladder.get(side) or []:
                vol = int(row.get("volume") or 0)
                mid = float(row.get("mid") or 0)
                premium = mid * 100 * vol
                if vol < min_volume and premium < min_premium:
                    continue
                scored = score_flow_print({**row, "premium_notional": premium}, spot=spot)
                scored.update(
                    {
                        "symbol": sym,
                        "expiry": expiry,
                        "dte": dte,
                        "spot": spot,
                        "delta_volume": int(row.get("delta_volume") or 0),
                        "delta_oi": int(row.get("delta_oi") or 0),
                        "premium_delta": float(row.get("premium_delta") or 0),
                    }
                )
                prints.append(scored)

    prints.sort(key=lambda p: (abs(float(p.get("flow_score") or 0)), float(p.get("premium_notional") or 0)), reverse=True)
    top = prints[:max_rows]
    golden = [p for p in top if p.get("tier") == "golden"]
    unusual = [p for p in top if p.get("tier") == "unusual"]
    aggressive = [p for p in top if p.get("tier") == "aggressive"]
    bull = sum(1 for p in top if p.get("sentiment") == "bullish")
    bear = sum(1 for p in top if p.get("sentiment") == "bearish")

    return {
        "prints": top,
        "golden": golden[:15],
        "unusual": unusual[:15],
        "aggressive": aggressive[:15],
        "counts": {
            "all": len(top),
            "golden": len(golden),
            "unusual": len(unusual),
            "aggressive": len(aggressive),
            "bullish": bull,
            "bearish": bear,
        },
        "note": (
            "ZeroLoss Flow proxy — Yahoo chain snapshot + vol/OI delta vs prior refresh. "
            "Not OPRA time & sales. Tiers: premium notional + unusualness + session delta."
        ),
    }


def build_flow_leaders(
    prints: list[dict[str, Any]],
    *,
    top_n: int = 20,
) -> list[dict[str, Any]]:
    """Aggregate flow prints by symbol → ranked leaders (Bullflow top-tickers proxy)."""
    by_sym: dict[str, dict[str, Any]] = {}
    tier_rank = {"golden": 3, "unusual": 2, "aggressive": 1}
    for p in prints or []:
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        agg = by_sym.setdefault(
            sym,
            {
                "symbol": sym,
                "net_flow_score": 0.0,
                "call_premium": 0.0,
                "put_premium": 0.0,
                "bullish_prints": 0,
                "bearish_prints": 0,
                "vol_gt_oi": False,
                "delta_volume": 0,
                "premium_delta": 0.0,
                "top_tier": "aggressive",
                "prints": 0,
            },
        )
        fs = float(p.get("flow_score") or 0)
        prem = float(p.get("premium_notional") or 0)
        agg["net_flow_score"] += fs
        agg["prints"] += 1
        if str(p.get("right") or "C").upper() == "P":
            agg["put_premium"] += prem
        else:
            agg["call_premium"] += prem
        if fs > 0:
            agg["bullish_prints"] += 1
        elif fs < 0:
            agg["bearish_prints"] += 1
        if p.get("vol_gt_oi"):
            agg["vol_gt_oi"] = True
        agg["delta_volume"] += int(p.get("delta_volume") or 0)
        agg["premium_delta"] += float(p.get("premium_delta") or 0)
        t = str(p.get("tier") or "aggressive")
        if tier_rank.get(t, 0) > tier_rank.get(str(agg["top_tier"]), 0):
            agg["top_tier"] = t

    leaders = list(by_sym.values())
    for row in leaders:
        net = float(row["net_flow_score"])
        if net > 5:
            row["sentiment"] = "bullish"
        elif net < -5:
            row["sentiment"] = "bearish"
        else:
            row["sentiment"] = "neutral"
        row["net_flow_score"] = round(net, 1)
        row["call_premium"] = round(float(row["call_premium"]), 2)
        row["put_premium"] = round(float(row["put_premium"]), 2)
        row["premium_delta"] = round(float(row["premium_delta"]), 2)

    leaders.sort(
        key=lambda r: (abs(float(r["net_flow_score"])), float(r["call_premium"]) + float(r["put_premium"])),
        reverse=True,
    )
    for i, row in enumerate(leaders[:top_n], start=1):
        row["rank"] = i
    return leaders[:top_n]
