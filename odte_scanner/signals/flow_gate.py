"""Tier-1 flow gate — calls need bullish flow leaders; puts need bearish."""
from __future__ import annotations

from typing import Any

from odte_scanner.signals.actions import ActionSignal


def _leader_map(flow_leaders: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in flow_leaders or []:
        sym = str(row.get("symbol") or "").upper()
        if sym:
            out[sym] = row
    return out


def apply_flow_gate(
    sig: ActionSignal,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    require_flow_confirm: bool = True,
    flow_leaders_top_n: int = 12,
    flow_min_net_score: float = 8.0,
    flow_min_tier: str = "aggressive",
    require_vol_gt_oi: bool = False,
) -> ActionSignal:
    """Demote BUY NOW → WAIT unless option-flow proxy aligns with call/put side."""
    if not require_flow_confirm or sig.action != "BUY_NOW":
        return sig
    sym = str(sig.symbol or "").upper()
    if not sym:
        return sig
    leaders = _leader_map(flow_leaders)
    row = leaders.get(sym)
    is_put = str(sig.right or "C").upper() == "P"
    tier_rank = {"aggressive": 0, "unusual": 1, "golden": 2}
    min_rank = tier_rank.get(str(flow_min_tier or "aggressive").lower(), 0)

    # Missing from leaders = soft pass (most names never hit top-N). Only hard-block
    # when flow IS present and conflicts with call/put side (Bullflow-style veto).
    if row is None:
        sig.detail = (
            f"{sig.detail} · flow n/a: {sym} not in top-{flow_leaders_top_n} leaders "
            "(tape/hist still gate)"
        )
        return sig

    rank = int(row.get("rank") or 999)
    if rank > flow_leaders_top_n:
        sig.detail = (
            f"{sig.detail} · flow n/a: rank #{rank} outside top {flow_leaders_top_n}"
        )
        return sig

    sentiment = str(row.get("sentiment") or "neutral")
    net = float(row.get("net_flow_score") or 0)
    top_tier = str(row.get("top_tier") or "aggressive").lower()
    if tier_rank.get(top_tier, 0) < min_rank:
        sig.detail = (
            f"{sig.detail} · flow n/a: tier {top_tier} < {flow_min_tier} (not a veto)"
        )
        return sig

    if is_put:
        if sentiment == "bullish" and net >= flow_min_net_score:
            sig.action = "WAIT"
            sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: put vs bullish flow "
                f"(got {sentiment}, net {net:+.0f})"
            )
            sig.strength = min(sig.strength, 48.0)
            return sig
        if sentiment != "bearish" or net > -flow_min_net_score:
            sig.detail = (
                f"{sig.detail} · flow soft: put preferred bearish "
                f"(got {sentiment}, net {net:+.0f})"
            )
            return sig
    else:
        if sentiment == "bearish" and net <= -flow_min_net_score:
            sig.action = "WAIT"
            sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: call vs bearish flow "
                f"(got {sentiment}, net {net:+.0f})"
            )
            sig.strength = min(sig.strength, 48.0)
            return sig
        if sentiment != "bullish" or net < flow_min_net_score:
            sig.detail = (
                f"{sig.detail} · flow soft: call preferred bullish "
                f"(got {sentiment}, net {net:+.0f})"
            )
            return sig

    if require_vol_gt_oi and not row.get("vol_gt_oi"):
        sig.detail = f"{sig.detail} · flow soft: no vol>OI on {sym}"
        return sig

    sig.detail = (
        f"{sig.detail} · flow OK: #{rank} {sentiment} net {net:+.0f} tier {top_tier}"
    )
    return sig
